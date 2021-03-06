# Copyright (c) 2019 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed
# under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

from enum import Enum, IntEnum
from concurrent.futures import ThreadPoolExecutor
import functools
import traceback

import mako.template

from util import (
    warning,
    existing_dir,
    not_none,
    info,
    merge_dicts,
    FluentIterable
)
from mailutil import _send_mail
from github.util import (
    github_cfg_for_hostname,
    GitHubRepositoryHelper,
    GitHubRepoBranch,
    _create_github_api_object,
)
from github.codeowners import CodeownersEnumerator, CodeOwnerEntryResolver

from concourse.factory import DefinitionFactory, RawPipelineDefinitionDescriptor
from concourse.enumerator import (
    DefinitionDescriptorPreprocessor,
    TemplateRetriever,
    GithubOrganisationDefinitionEnumerator,
)

from concourse import client
import concourse.client.model


def replicate_pipelines(
    cfg_set,
    concourse_cfg,
    job_mapping,
    template_path,
    template_include_dir,
    unpause_pipelines: bool=True,
    expose_pipelines: bool=True,
):
    definition_enumerators = [
        GithubOrganisationDefinitionEnumerator(
            job_mapping=job_mapping,
            cfg_set=cfg_set,
        ),
    ]

    preprocessor = DefinitionDescriptorPreprocessor()
    template_retriever = TemplateRetriever(template_path=template_path)
    renderer = Renderer(
        template_retriever=template_retriever,
        template_include_dir=template_include_dir,
        cfg_set=cfg_set,
    )

    deployer = ConcourseDeployer(
        unpause_pipelines=unpause_pipelines,
        expose_pipelines=expose_pipelines,
    )

    result_processor = ReplicationResultProcessor(
        cfg_set=cfg_set,
    )

    replicator = PipelineReplicator(
        definition_enumerators=definition_enumerators,
        descriptor_preprocessor=preprocessor,
        definition_renderer=renderer,
        definition_deployer=deployer,
        result_processor=result_processor,
    )

    return replicator.replicate()


class Renderer(object):
    def __init__(self, template_retriever, template_include_dir, cfg_set):
        self.template_retriever = template_retriever
        if template_include_dir:
            template_include_dir = os.path.abspath(template_include_dir)
            self.template_include_dir = os.path.abspath(template_include_dir)
            from mako.lookup import TemplateLookup
            self.lookup = TemplateLookup([template_include_dir])
            self.cfg_set = cfg_set

    def render(self, definition_descriptor):
        try:
            definition_descriptor = self._render(definition_descriptor)
            info('rendered pipeline {pn}'.format(pn=definition_descriptor.pipeline_name))
            return RenderResult(
                definition_descriptor,
                render_status=RenderStatus.SUCCEEDED,
            )
        except Exception:
            warning(
                f"erroneous pipeline definition '{definition_descriptor.pipeline_name}' "
                f"in repository '{definition_descriptor.main_repo.get('path')}'"
            )
            traceback.print_exc()
            return RenderResult(
                definition_descriptor,
                render_status=RenderStatus.FAILED,
                error_details=traceback.format_exc(),
            )

    def _render(self, definition_descriptor):
        effective_definition = definition_descriptor.pipeline_definition

        # handle inheritance
        for override in definition_descriptor.override_definitions:
            effective_definition = merge_dicts(effective_definition, override)

        template_name = definition_descriptor.template_name()
        template_contents = self.template_retriever.template_contents(template_name)

        pipeline_name = definition_descriptor.pipeline_name

        # support declaring jobs by either 'jobs' or 'variants'
        # TODO: Add deprecation message for old 'variants' syntax.
        jobs = effective_definition.get('jobs', {})
        variants = effective_definition.get('variants', {})
        if jobs and variants:
            raise RuntimeError(
                f"Both 'jobs' and 'variants' are defined in pipeline '{pipeline_name}'"
            )

        pipeline_definition = RawPipelineDefinitionDescriptor(
            name=pipeline_name,
            base_definition=effective_definition.get('base_definition', {}),
            variants=jobs or variants,
            template=template_name,
        )

        factory = DefinitionFactory(raw_definition_descriptor=pipeline_definition)
        pipeline_metadata = dict()
        pipeline_metadata['definition'] = factory.create_pipeline_definition()
        pipeline_metadata['name'] = pipeline_definition.name
        pipeline_metadata['target_team'] = definition_descriptor.concourse_target_team
        generated_model = pipeline_metadata.get('definition')

        # determine pipeline name (if there is main-repo, append the configured branch name)
        for variant in pipeline_metadata.get('definition').variants():
            # hack: take the first "main_repository" we find
            if not variant.has_main_repository():
                continue
            main_repo = variant.main_repository()
            pipeline_metadata['pipeline_name'] = '-'.join(
                [pipeline_definition.name, main_repo.branch()]
            )
            break
        else:
            # fallback in case no main_repository was found
            pipeline_metadata['pipeline_name'] = pipeline_definition.name
            main_repo = None

        t = mako.template.Template(template_contents, lookup=self.lookup)

        definition_descriptor.pipeline = t.render(
                instance_args=generated_model,
                config_set=self.cfg_set,
                pipeline=pipeline_metadata,
        )

        return definition_descriptor


class RenderStatus(Enum):
    SUCCEEDED = 0
    FAILED = 1


class RenderResult(object):
    def __init__(
        self,
        definition_descriptor,
        render_status,
        error_details=None,
    ):
        self.definition_descriptor = not_none(definition_descriptor)
        self.render_status = not_none(render_status)
        self.error_details = error_details


class DeployStatus(IntEnum):
    SUCCEEDED = 1
    FAILED = 2
    SKIPPED = 4
    CREATED = 8


class DeployResult(object):
    def __init__(
        self,
        definition_descriptor,
        deploy_status,
        error_details=None,
    ):
        self.definition_descriptor = not_none(definition_descriptor)
        self.deploy_status = not_none(deploy_status)
        self.error_details = error_details


class DefinitionDeployer(object):
    def deploy(self, definition_descriptor, pipeline):
        raise NotImplementedError('subclasses must overwrite')


class FilesystemDeployer(DefinitionDeployer):
    def __init__(self, base_dir):
        self.base_dir = existing_dir(base_dir)

    def deploy(self, definition_descriptor):
        try:
            with open(os.path.join(self.base_dir, definition_descriptor.pipeline_name), 'w') as f:
                f.write(definition_descriptor.pipeline)
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SUCCEEDED,
            )
        except Exception as e:
            warning(e)
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.FAILED,
            )


class ConcourseDeployer(DefinitionDeployer):
    def __init__(
        self,
        unpause_pipelines: bool,
        expose_pipelines: bool=True
    ):
        self.unpause_pipelines = unpause_pipelines
        self.expose_pipelines = expose_pipelines

    def deploy(self, definition_descriptor):
        pipeline_definition = definition_descriptor.pipeline
        pipeline_name = definition_descriptor.pipeline_name
        try:
            api = client.from_cfg(
                concourse_cfg=definition_descriptor.concourse_target_cfg,
                team_name=definition_descriptor.concourse_target_team,
            )
            response = api.set_pipeline(
                name=pipeline_name,
                pipeline_definition=pipeline_definition
            )
            info(
                'Deployed pipeline: ' + pipeline_name +
                ' to team: ' + definition_descriptor.concourse_target_team
            )
            if self.unpause_pipelines:
                api.unpause_pipeline(pipeline_name=pipeline_name)
            if self.expose_pipelines:
                api.expose_pipeline(pipeline_name=pipeline_name)

            deploy_status = DeployStatus.SUCCEEDED
            if response is concourse.client.model.SetPipelineResult.CREATED:
                deploy_status |= DeployStatus.CREATED
            elif response is concourse.client.model.SetPipelineResult.UPDATED:
                pass
            else:
                raise NotImplementedError

            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=deploy_status,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            warning(e)
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.FAILED,
                error_details=traceback.format_exc(),
            )


class ReplicationResultProcessor(object):
    def __init__(self, cfg_set):
        self._cfg_set = cfg_set

    def process_results(self, results):
        # collect pipelines by concourse target (concourse_cfg, team_name) as key
        concourse_target_results = {}
        for result in results:
            definition_descriptor = result.definition_descriptor
            concourse_target_key = definition_descriptor.concourse_target_key()
            if concourse_target_key not in concourse_target_results:
                concourse_target_results[concourse_target_key] = set()
            concourse_target_results[concourse_target_key].add(result)

        for concourse_target_key, concourse_results in concourse_target_results.items():
            # TODO: implement eq for concourse_cfg
            concourse_cfg, concourse_team = next(iter(
                concourse_results)).definition_descriptor.concourse_target()
            concourse_results = concourse_target_results[concourse_target_key]
            concourse_api = client.from_cfg(
                concourse_cfg=concourse_cfg,
                team_name=concourse_team,
            )
            # find pipelines to remove
            deployed_pipeline_names = set(map(
                lambda r: r.definition_descriptor.pipeline_name, concourse_results
            ))

            pipelines_to_remove = set(concourse_api.pipelines()) - deployed_pipeline_names

            for pipeline_name in pipelines_to_remove:
                info('removing pipeline: {p}'.format(p=pipeline_name))
                concourse_api.delete_pipeline(pipeline_name)

            # trigger resource checks in new pipelines
            self._initialise_new_pipeline_resources(concourse_api, concourse_results)

            # order pipelines alphabetically
            pipeline_names = list(concourse_api.pipelines())
            pipeline_names.sort()
            concourse_api.order_pipelines(pipeline_names)

        # evaluate results
        failed_descriptors = [
            d for d in results
            if not d.deploy_status & DeployStatus.SUCCEEDED
        ]

        failed_count = len(failed_descriptors)

        info('Successfully replicated {d} pipeline(s)'.format(d=len(results) - failed_count))

        if failed_count == 0:
            return True

        warning('Errors occurred whilst replicating {d} pipeline(s):'.format(
            d=failed_count,
        )
        )

        all_notifications_succeeded = True
        for failed_descriptor in failed_descriptors:
            warning(failed_descriptor.definition_descriptor.pipeline_name)
            try:
                self._notify_broken_definition_owners(failed_descriptor)
            except Exception:
                warning('an error occurred whilst trying to send error notifications')
                traceback.print_exc()
                all_notifications_succeeded = False

        # signall error only if error notifications failed
        return all_notifications_succeeded

    def _notify_broken_definition_owners(self, failed_descriptor):
        definition_descriptor = failed_descriptor.definition_descriptor
        main_repo = definition_descriptor.main_repo
        github_cfg = github_cfg_for_hostname(self._cfg_set, main_repo['hostname'])
        github_api = _create_github_api_object(github_cfg)
        repo_owner, repo_name = main_repo['path'].split('/')

        githubrepobranch = GitHubRepoBranch(
            github_config=github_cfg,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=main_repo['branch'],
        )

        repo_helper = GitHubRepositoryHelper.from_githubrepobranch(
            githubrepobranch=githubrepobranch,
        )

        codeowners_enumerator = CodeownersEnumerator()
        codeowners_resolver = CodeOwnerEntryResolver(github_api=github_api)
        recipients = set(codeowners_resolver.resolve_email_addresses(
            codeowners_enumerator.enumerate_remote_repo(github_repo_helper=repo_helper)
        ))

        # in case no codeowners are available, resort to using the committer
        if not recipients:
            head_commit = repo_helper.repository.commit(main_repo['branch'])
            user_ids = {
                user_info.get('login')
                for user_info
                in (head_commit.committer, head_commit.author)
                if user_info.get('login')
            }
            for user_id in user_ids:
                user = github_api.user(user_id)
                if user.email:
                    recipients.add(user.email)

        info(f'Sending notification e-mail to {recipients} ({main_repo["path"]})')
        email_cfg = self._cfg_set.email()
        _send_mail(
            email_cfg=email_cfg,
            recipients=recipients,
            subject='Your pipeline definition in {repo} is erroneous'.format(
                repo=main_repo['path'],
            ),
            mail_template='Error details:\n' + str(failed_descriptor.error_details),
        )

    def _initialise_new_pipeline_resources(self, concourse_api, results):
        newly_deployed_pipeline_names = map(
            lambda result: result.definition_descriptor.pipeline_name,
            filter(
                lambda result: result.deploy_status & DeployStatus.CREATED,
                results,
            )
        )
        for pipeline_name in newly_deployed_pipeline_names:
            info('unpausing new pipeline {p}'.format(p=pipeline_name))
            concourse_api.unpause_pipeline(pipeline_name)

            info('triggering initial resource check for pipeline {p}'.format(p=pipeline_name))

            trigger_pipeline_resource_check = functools.partial(
                concourse_api.trigger_resource_check,
                pipeline_name=pipeline_name,
            )

            FluentIterable(concourse_api.pipeline_resources(pipeline_name)) \
            .filter(lambda resource: resource.has_webhook_token()) \
            .map(lambda resource: trigger_pipeline_resource_check(resource_name=resource.name)) \
            .as_list()


class PipelineReplicator(object):
    def __init__(
            self,
            definition_enumerators,
            descriptor_preprocessor,
            definition_renderer,
            definition_deployer,
            result_processor=None,
        ):
        self.definition_enumerators = definition_enumerators
        self.descriptor_preprocessor = descriptor_preprocessor
        self.definition_renderer = definition_renderer
        self.definition_deployer = definition_deployer
        self.result_processor = result_processor

    def _enumerate_definitions(self):
        for enumerator in self.definition_enumerators:
            yield from enumerator.enumerate_definition_descriptors()

    def _process_definition_descriptor(self, definition_descriptor):
        if definition_descriptor.exception:
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SKIPPED,
                error_details=definition_descriptor.exception,
            )

        preprocessed = self.descriptor_preprocessor.process_definition_descriptor(
                definition_descriptor
        )
        result = self.definition_renderer.render(preprocessed)

        if result.render_status == RenderStatus.SUCCEEDED:
            deploy_result = self.definition_deployer.deploy(result.definition_descriptor)
        else:
            deploy_result = DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SKIPPED,
                error_details=result.error_details,
            )
        return deploy_result

    def _replicate(self):
        executor = ThreadPoolExecutor(max_workers=8)
        yield from executor.map(
            self._process_definition_descriptor,
            self._enumerate_definitions(),
        )

    def replicate(self):
        results = []
        for result in self._replicate():
            results.append(result)

        if self.result_processor:
            return self.result_processor.process_results(results)
