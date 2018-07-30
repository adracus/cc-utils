# Copyright (c) 2018 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
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

from abc import abstractmethod
from collections import namedtuple
import git
from git.exc import GitError
from github.util import GitHubRepositoryHelper
from pydash import _
import re
from semver import parse_version_info

from util import info, warning, fail, verbose, existing_dir
from product.model import ComponentName
from model.base import ModelValidationError

ReleaseNote = namedtuple('ReleaseNote', [
    "category_id",
    "target_group_id",
    "text",
    "reference_is_pr",
    "reference_id",
    "user_login",
    "source_repo",
    "is_current_repo",
    "component_name"
])

def create_release_note_obj(
    category_id: str,
    target_group_id: str,
    text: str,
    reference_is_pr: str,
    reference_id: str,
    user_login: str,
    source_repo: str,
    is_current_repo: bool
)->ReleaseNote:

    if reference_id:
        reference_id=str(reference_id)

    return ReleaseNote(
        category_id=category_id,
        target_group_id=target_group_id,
        text=text,
        reference_is_pr=reference_is_pr,
        reference_id=reference_id,
        user_login=user_login,
        source_repo=source_repo,
        is_current_repo=is_current_repo,
        component_name=ComponentName(name=source_repo)
    )

def generate_release_notes(
    repo_dir: str,
    helper: GitHubRepositoryHelper,
    repository_branch: str,
    commit_range: str=None
):
    repo = git.Repo(existing_dir(repo_dir))

    current_repo_name = ComponentName.from_github_repo_url(helper.repository.html_url).name()

    if not commit_range:
        commit_range = calculate_range(repository_branch, repo, helper)
    pr_numbers = fetch_pr_numbers_in_range(repo, commit_range)
    release_note_objs = fetch_release_notes_from_prs(helper, pr_numbers, current_repo_name)
    release_notes_str = MarkdownRenderer(release_note_objs).render()

    info(release_notes_str)
    return release_notes_str

class Renderer(object):
    Node = namedtuple("Node", ["identifier", "title", "nodes", "matches_rn_field"])

    target_groups = \
        Node(
            identifier='user',
            title='USER',
            nodes=None,
            matches_rn_field='target_group_id'
        ), \
        Node(
            identifier='operator',
            title='OPERATOR',
            nodes=None,
            matches_rn_field='target_group_id'
        )
    categories = \
        Node(
            identifier='noteworthy',
            title='Most notable changes',
            nodes=target_groups,
            matches_rn_field='category_id'
        ), \
        Node(
            identifier='improvement',
            title='Improvements',
            nodes=target_groups,
            matches_rn_field='category_id'
        )

    def __init__(self, release_note_objs: list):
        self.release_note_objs = release_note_objs

    def render(self)->str:
        origin_nodes = _\
            .chain(self.release_note_objs)\
            .sort_by(lambda rn_obj: rn_obj.component_name.github_repo())\
            .uniq_by(lambda rn_obj: rn_obj.source_repo)\
            .map(lambda rn_obj: Renderer.Node(
                identifier=rn_obj.source_repo,
                title='[{origin_name}]'.format(origin_name=rn_obj.component_name.github_repo()),
                nodes=Renderer.categories,
                matches_rn_field='source_repo'
            ))\
            .value()

        md_lines = self._nodes_to_lines(
            nodes=origin_nodes,
            level=1,
            release_note_objs=self.release_note_objs
        )

        if md_lines:
            return '\n'.join(md_lines)
        else: # fallback
            return 'no release notes available'

    @abstractmethod
    def _nodes_to_lines(
        self,
        nodes: list,
        level: int,
        release_note_objs: list
    )->list:
        pass

class MarkdownRenderer(Renderer):

    def _nodes_to_lines(
        self,
        nodes: list,
        level: int,
        release_note_objs: list
    )->list:
        lines = MarkdownRenderer._nodes_to_markdown_lines(
            nodes=nodes,
            level=level,
            release_note_objs=release_note_objs
        )
        return lines

    @staticmethod
    def _get_header_suffix(
        rn_obj: ReleaseNote
    )->str:
        header_suffix = ''
        if rn_obj.user_login or rn_obj.reference_id:
            header_suffix_list = list()
            cn = rn_obj.component_name
            if rn_obj.reference_id:
                if rn_obj.reference_is_pr:
                    reference_prefix = '#'
                    reference_link = 'https://{source_repo}/pull/{ref_id}'.format(
                        source_repo=rn_obj.source_repo,
                        ref_id=rn_obj.reference_id
                    )
                else: # commit
                    if not rn_obj.is_current_repo:
                        reference_prefix = '@'
                    else:
                        # for the current repo we use gitHub's feature to auto-link to references,
                        # hence in case of commits we don't need a prefix
                        reference_prefix = ''
                    reference_link = 'https://{source_repo}/commit/{ref_id}'.format(
                        source_repo=rn_obj.source_repo,
                        ref_id=rn_obj.reference_id
                )

                reference = '{reference_prefix}{ref_id}'.format(
                    reference_prefix=reference_prefix,
                    ref_id=rn_obj.reference_id,
                )

                if rn_obj.is_current_repo:
                    header_suffix_list.append(reference)
                else:
                    header_suffix_list.append(
                        '[{org}/{repo}{reference}]({ref_link})'.format(
                            org=cn.github_organisation(),
                            repo=cn.github_repo(),
                            reference=reference,
                            ref_link=reference_link
                        )
                    )
            if rn_obj.user_login:
                header_suffix_list.append('[@{u}](https://{github_host}/{u})'.format(
                    u=rn_obj.user_login,
                    github_host=cn.github_host()
                ))
            header_suffix = ' ({s})'.format(
                s=', '.join(header_suffix_list)
            )
        return header_suffix

    @staticmethod
    def _build_bullet_point_head(
        line: str,
        tag: str,
        rn_obj: ReleaseNote
    )->str:
        header_suffix = MarkdownRenderer._get_header_suffix(rn_obj)

        return '* *[{tag}]* {rls_note_line}{header_suffix}'.format(
                    tag=tag,
                    rls_note_line=line,
                    header_suffix=header_suffix
                )
    @staticmethod
    def _to_md_bullet_points(
        tag: str,
        rn_objs: list,
    ):
        bullet_points = list()
        for rn_obj in rn_objs:
            for i, rls_note_line in enumerate(rn_obj.text.splitlines()):
                if i == 0:
                    bullet_points.append(
                        MarkdownRenderer._build_bullet_point_head(
                            line=rls_note_line,
                            tag=tag,
                            rn_obj=rn_obj
                        )
                    )
                else:
                    bullet_points.append('  * {rls_note_line}'.format(
                        rls_note_line=rls_note_line
                    ))
        return bullet_points

    @staticmethod
    def _nodes_to_markdown_lines(
        nodes: list,
        level: int,
        release_note_objs: list
    ) -> list:
        md_lines = list()
        for node in nodes:
            filtered_rn_objects = _.filter(
                release_note_objs,
                lambda rn: node.identifier == _.get(rn, node.matches_rn_field)
            )
            if not filtered_rn_objects:
                continue
            if node.nodes:
                tmp_md_lines = MarkdownRenderer._nodes_to_markdown_lines(
                    nodes=node.nodes,
                    level=level + 1,
                    release_note_objs=filtered_rn_objects
                )
                skip_title = False
            else:
                tmp_md_lines = MarkdownRenderer._to_md_bullet_points(
                    tag=node.title,
                    rn_objs=filtered_rn_objects
                )
                # title is used as bullet point tag -> no need for additional title
                skip_title = True

            # only add title if there are lines below the title
            if tmp_md_lines:
                if not skip_title:
                    md_lines.append('{hashtags} {title}'.format(
                        hashtags=_.repeat('#', level),
                        title=node.title
                    ))
                md_lines.extend(tmp_md_lines)
        return md_lines

def calculate_range(
    repository_branch: str,
    repo: git.Repo,
    helper: GitHubRepositoryHelper,
) -> str:

    branch_head = repo.rev_parse('refs/remotes/origin/' + repository_branch)
    if not branch_head:
        fail('could not determine branch head of {branch} branch'.format(
            branch=repository_branch
        ))
    range_start = _.head(reachable_release_tags_from_commit(helper, repo, branch_head))

    range_end = None
    try:
        # better readable range_end by describing head commit
        range_end = repo.git.describe(branch_head)
    except GitError:
        range_end = branch_head.hexsha

    commit_range = "{start}..{end}".format(start=range_start, end=range_end)
    return commit_range

def release_tags(
    helper: GitHubRepositoryHelper,
    repo: git.Repo
) -> list:
    def is_valid_semver(tag_name):
        try:
            parse_version_info(tag_name)
            return True
        except ValueError:
            warning('{tag} is not a valid SemVer string'.format(tag=tag_name))
            return False

    release_tags = helper.release_tags()
    # you can remove the directive to disable the undefined-variable error once pylint is updated
    # with fix https://github.com/PyCQA/pylint/commit/db01112f7e4beadf7cd99c5f9237d580309f0494
    # included
    # pylint: disable=undefined-variable
    tags = _ \
        .chain(repo.tags) \
        .map(lambda tag: {"tag": tag.name, "commit": tag.commit.hexsha}) \
        .filter(lambda item: _.find(release_tags, lambda el: el == item['tag'])) \
        .filter(lambda item: is_valid_semver(item['tag'])) \
        .key_by('commit') \
        .map_values('tag') \
        .value()
    # pylint: enable=undefined-variable
    return tags

def reachable_release_tags_from_commit(
    helper: GitHubRepositoryHelper,
    repo: git.Repo,
    commit: git.objects.Commit
) -> list:
    tags = release_tags(helper, repo)

    visited = set()
    queue = list()
    queue.append(commit)
    visited.add(commit.hexsha)

    reachable_tags = list()

    while queue:
        commit = queue.pop(0)
        if commit.hexsha in tags:
            reachable_tags.append(tags[commit.hexsha])
        not_visited_parents = _.filter(commit.parents,
            lambda parent_commit: not parent_commit.hexsha in visited
        )
        if not_visited_parents:
            queue.extend(not_visited_parents)
            visited |= set(_.map(not_visited_parents, lambda commit: commit.hexsha))

    reachable_tags.sort(key=lambda t: parse_version_info(t), reverse=True)

    if not reachable_tags:
        warning('no release tag found, falling back to root commit')
        root_commits = repo.iter_commits(rev=commit, max_parents=0)
        root_commit = next(root_commits, None)
        if not root_commit:
            fail('could not determine root commit from rev {rev}'.format(rev=commit.hexsha))
        if next(root_commits, None):
            fail(
                'cannot determine range for release notes. Repository has multiple root commits. '
                'Specify range via commit_range parameter.'
            )
        reachable_tags.append(root_commit.hexsha)

    return reachable_tags


def fetch_pr_numbers_in_range(
    repo: git.Repo,
    commit_range: str
) -> set:
    info('git log {range}'.format(range=commit_range))
    gitLogs = repo.git.log(commit_range, pretty='%s').splitlines()
    pr_numbers = set()
    for commitMessage in gitLogs:
        if commitMessage.startswith('Merge pull'):
            pr_number = _.head(re.findall(r"#(\d+|$)", commitMessage))
            if pr_number:
                pr_numbers.add(pr_number)

    verbose('Merged pull request numbers in range {range}: {pr_numbers}'.format(
        range=commit_range,
        pr_numbers=pr_numbers
    ))
    return pr_numbers

def fetch_release_notes_from_prs(
    helper: GitHubRepositoryHelper,
    pr_numbers_in_range: set,
    current_repo:str
) -> list:
    # we should consider adding a release-note label to the PRs
    # to reduce the number of search results
    prs_iter = helper.search_issues_in_repo('type:pull is:closed')

    release_notes = list()
    for pr_iter in prs_iter:
        pr_dict = pr_iter.as_dict()

        pr_number = pr_dict['number']
        if not str(pr_number) in pr_numbers_in_range:
            continue

        release_notes_pr = extract_release_notes(
            pr_number=pr_number,
            text=pr_dict['body'],
            user_login=_.get(pr_dict, 'user.login'),
            current_repo=current_repo
        )
        if not release_notes_pr:
            continue

        release_notes.extend(release_notes_pr)
    return release_notes

def extract_release_notes(
    pr_number: int,
    text: str,
    user_login: str,
    current_repo: str
) -> list:
    release_notes = list()

    r = re.compile(
        r"``` *(?P<category>improvement|noteworthy) (?P<target_group>user|operator)"
        "( (?P<source_repo>\S+/\S+/\S+)(( (?P<reference_type>#|\$)(?P<reference_id>\S+))?"
        "( @(?P<user>\S+))?)( .*?)?|( .*?)?)\r?\n(?P<text>.*?)\n```",
        re.MULTILINE | re.DOTALL
    )
    for m in r.finditer(text):
        code_block = m.groupdict()

        text = _.trim(code_block['text'])
        if not text or 'none' == text.lower():
            continue

        category = code_block['category']
        target_group = code_block['target_group']
        source_repo = code_block['source_repo']
        if source_repo:
            reference_is_pr = code_block['reference_type'] == '#'
            reference_id = code_block['reference_id'] or None
            user_login = code_block['user'] or None
        else:
            source_repo = current_repo
            reference_is_pr = True
            reference_id = pr_number

        try:
            release_notes.append(create_release_note_obj(
                category_id=category,
                target_group_id=target_group,
                text=text,
                reference_is_pr=reference_is_pr,
                reference_id=reference_id,
                user_login=user_login,
                source_repo=source_repo,
                is_current_repo=current_repo == source_repo
            ))
        except ModelValidationError:
            warning('skipping invalid origin repository: {source_repo}'.format(
                source_repo=source_repo
            ))
            continue
    return release_notes