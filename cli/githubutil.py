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

import semver
from util import (
    CliHint,
    ctx,
)
from gitutil import GitHelper
from github.util import (
    GitHubRepositoryHelper,
    GitHubRepoBranch,
    _create_github_api_object,
    _create_team,
    _add_user_to_team,
    _add_all_repos_to_team
)
from github.release_notes.util import (
    ReleaseNotes,
)
from version import partition_by_major_and_minor


def assign_github_team_to_repo(
    github_cfg_name: str,
    github_org_name: str,
    auth_token: CliHint(help="Token from an org admin user. Token must have 'admin:org' scope"),
    team_name: str='ci'
):
    '''
    Assign team 'team_name' to all repositories in organization 'github_org_name' and
    give the team admin rights on those repositories. The team will be created if it does not exist
    and the technical github user (from github_cfg_name) will be assigned to the team.
    The token of the technical github user must have the privilege to create webhooks
    (scope admin:repo_hook)
    'auth_token'  must grant 'admin:org' privileges.
    '''
    cfg_factory = ctx().cfg_factory()
    github_cfg = cfg_factory.github(github_cfg_name)
    github_username = github_cfg.credentials().username()

    # overwrite auth_token
    github_cfg.credentials().set_auth_token(auth_token=auth_token)

    github = _create_github_api_object(
        github_cfg=github_cfg,
    )

    _create_team(
        github=github,
        organization_name=github_org_name,
        team_name=team_name
    )

    _add_user_to_team(
        github=github,
        organization_name=github_org_name,
        team_name=team_name,
        user_name=github_username
    )

    _add_all_repos_to_team(
        github=github,
        organization_name=github_org_name,
        team_name=team_name
    )


def generate_release_notes_cli(
    repo_dir: str,
    github_cfg_name: str,
    github_repository_owner: str,
    github_repository_name: str,
    repository_branch: str,
    commit_range: str=None
):
    github_cfg = ctx().cfg_factory().github(github_cfg_name)

    githubrepobranch = GitHubRepoBranch(
        github_config=github_cfg,
        repo_owner=github_repository_owner,
        repo_name=github_repository_name,
        branch=repository_branch,
    )

    helper = GitHubRepositoryHelper.from_githubrepobranch(
        githubrepobranch=githubrepobranch,
    )
    git_helper = GitHelper.from_githubrepobranch(
        repo_path=repo_dir,
        githubrepobranch=githubrepobranch,
    )

    ReleaseNotes.create(
        github_helper=helper,
        git_helper=git_helper,
        repository_branch=repository_branch,
        commit_range=commit_range
    ).to_markdown()


def release_note_blocks_cli(
    repo_dir: str,
    github_cfg_name: str,
    github_repository_owner: str,
    github_repository_name: str,
    repository_branch: str=None,
    commit_range: str=None
):
    github_cfg = ctx().cfg_factory().github(github_cfg_name)

    githubrepobranch = GitHubRepoBranch(
        github_config=github_cfg,
        repo_owner=github_repository_owner,
        repo_name=github_repository_name,
        branch=repository_branch,
    )

    helper = GitHubRepositoryHelper.from_githubrepobranch(
        githubrepobranch=githubrepobranch,
    )
    git_helper = GitHelper.from_githubrepobranch(
        repo_path=repo_dir,
        githubrepobranch=githubrepobranch,
    )

    ReleaseNotes.create(
        github_helper=helper,
        git_helper=git_helper,
        repository_branch=repository_branch,
        commit_range=commit_range
    ).release_note_blocks()


def cleanup_draft_releases(
    github_cfg_name: str,
    github_repository_owner: str,
    github_repository_name: str,
    greatest_release_version: CliHint(
        help=(
            'Greatest version (according to Semver) up to which (exclusive) '
            'to consider draft releases.'
        ),
    ),
    dry_run: bool=True,
):
    '''Remove outdated draft releases from a GitHub repository.

    Scans all releases on GitHub and removes outdated draft releases. A draft release is considered
    outdated iff:
        1: it is preceding the greatest (according to Semver) release AND
            2a: it is NOT a hotfix draft release AND
            3a: there are no hotfix draft releases with the same major and minor version
            OR
            2b: it is a hotfix draft release AND
            3b: there is a hotfix draft release with the same major and minor version succeeding it

    This is achieved by partitioning the releases according to their major and minor version
    combined. Partitions are then checked:
        - if there is only a single release in a partition it is either a hotfix release (keep)
        or it is not (delete)
        - if there are multiple releases in a partition, keep only the greatest (according
        to semver)
    '''
    github_cfg = ctx().cfg_factory().github(github_cfg_name)
    githubrepobranch = GitHubRepoBranch(
        github_config=github_cfg,
        repo_owner=github_repository_owner,
        repo_name=github_repository_name,
        branch=None,
    )

    github_helper = GitHubRepositoryHelper.from_githubrepobranch(
        githubrepobranch=githubrepobranch,
    )

    greatest_semver = semver.parse_version_info(greatest_release_version)

    def _is_autogenerated_draft_release(release):
        try:
            release_semver = semver.parse_version_info(release.name)
        except Exception:
            return False
        if release_semver.prerelease != 'draft':
            return False
        return True

    autogenerated_draft_releases = [
        release for release in github_helper.repository.releases()
        if release.draft and _is_autogenerated_draft_release(release)
    ]

    version_infos = [
        semver.parse_version_info(release.name) for release in autogenerated_draft_releases
    ]

    def _process_partition(partition):
        if len(partition) == 1:
            version_info = partition.pop()
            if version_info < greatest_semver and version_info.patch == 0:
                yield version_info
        else:
            sorted_partition = sorted(partition, reverse=True)
            yield from [
                version_info
                for version_info in sorted_partition[1:]
                if version_info < greatest_semver
            ]

    outdated_version_infos = list()
    for partition in partition_by_major_and_minor(version_infos):
        outdated_version_infos.extend(_process_partition(partition))

    outdated_draft_releases = [
        release
        for release in autogenerated_draft_releases
        if semver.parse_version_info(release.name) in outdated_version_infos
    ]

    if dry_run:
        print(
            "This is a dry-run. The following operations will be performed if the '--no-dry-run'"
            " flag is set:"
        )
    for release in outdated_draft_releases:
        print(f"Deleting release '{release.name}'")
        if not dry_run:
            release.delete()
