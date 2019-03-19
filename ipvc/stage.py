import os
import io
import json
import sys
import tempfile
from subprocess import call
from pathlib import Path
from datetime import datetime

import ipfsapi
from ipvc.common import CommonAPI, atomic


class StageAPI(CommonAPI):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _get_relative_paths(self, fs_paths, fs_repo_root):
        fs_paths = fs_paths if isinstance(fs_paths, list) else [fs_paths]
        for fs_path in fs_paths:
            fs_path = Path(os.path.abspath(fs_path))
            try: 
                yield fs_path.relative_to(fs_repo_root)
            except:
                # Doesn't start with workspace_root
                self.print_err(f'Path outside workspace {fs_path}')
                raise

    def _notify_pull_merge(self, fs_repo_root, branch):
        mfs_merge_parent = self.get_mfs_path(fs_repo_root, branch, branch_info='merge_parent')
        try:
            self.ipfs.files_stat(mfs_merge_parent)
            self.print(('NOTE: you are in the merge conflict state, the next '
                        'commit will be the merge commit. To abort merge, run '
                        '`ipvc branch pull --abort`\n'))
            return True
        except:
            return False

    @atomic
    def add(self, fs_paths=None):
        """ Add the path to ipfs, and replace the stage files at that path with
        the new hash.
        """
        self.common()
        fs_paths = self.fs_cwd if fs_paths is None else fs_paths
        changes = []
        for fs_path_relative in self._get_relative_paths(fs_paths, self.fs_repo_root):
            changes = changes + self.add_ref_changes_to_ref(
                'workspace', 'stage', fs_path_relative)

        if len(changes) == 0:
            self.print('No changes')
        else:
            self.print('Changes:')
            self.print_changes(changes)
        return changes

    @atomic
    def remove(self, fs_paths):
        """ Add the path to ipfs, and replace the stage files at that path with
        the new hash.
        """
        self.common()
        changes = []
        for fs_path_relative in self._get_relative_paths(fs_paths, self.fs_repo_root):
            changes = changes + self.add_ref_changes_to_ref(
                'head', 'stage', fs_path_relative)

        if len(changes) == 0:
            self.print('No changes')
        else:
            self.print('Changes:')
            self.print_changes(changes)
        return changes

    @atomic
    def status(self):
        """ Show diff between workspace and stage, and between stage and head """
        self.common()
        self._notify_pull_merge(self.fs_repo_root, self.active_branch)

        head_stage_changes, *_ = self.get_mfs_changes(
            'head/bundle/files', 'stage/bundle/files')
        if len(head_stage_changes) == 0:
            self.print('No staged changes')
        else:
            self.print('Staged:')
            self.print_changes(head_stage_changes)
            self.print('-'*80)

        stage_workspace_changes, *_ = self.get_mfs_changes(
            'stage/bundle/files', 'workspace/bundle/files')
        if len(stage_workspace_changes) == 0:
            self.print('No unstaged changes')
        else:
            self.print('Unstaged:')
            self.print_changes(stage_workspace_changes)

        return head_stage_changes, stage_workspace_changes

    @atomic
    def commit(self, message=None, commit_metadata=None):
        """ Creates a new commit with the staged changes and returns new commit hash

        If commit_metadata is provided instead of message, then it will be used instead
        of generating new metadata
        """
        self.common()

        mfs_merge_parent = self.get_mfs_path(self.fs_repo_root, self.active_branch,
                                             branch_info='merge_parent')
        mfs_replay_offset = self.get_mfs_path(self.fs_repo_root, self.active_branch,
                                             branch_info='replay_offset')
        is_merge = False
        try:
            self.ipfs.files_stat(mfs_merge_parent)
            is_merge = True
        except:
            pass

        is_replay = False
        try:
            self.ipfs.files_stat(mfs_replay_offset)
            is_replay = True
        except:
            pass

        changes = self._diff_changes(Path('@stage'), Path('@head'))
        if not (is_merge or is_replay) and len(changes) == 0:
            self.print_err('Nothing to commit')
            raise RuntimeError

        # Create commit_metadata
        if commit_metadata is None:
            if message is None:
                EDITOR = os.environ.get('EDITOR','vim')
                initial_message = (
                    '\n\n# Write your commit message above, then save and exit the editor.\n'
                    '# Lines starting with # will be ignored.\n\n'
                    '# To change the default editor, change the EDITOR environment variable.'
                )
                # Get the diff to stage from head
                diff_str = self._format_changes(changes, files=False)
                # Add comments to all lines
                diff_str = diff_str.replace('\n', '\n# ')
                if len(diff_str) > 0:
                    # Prepend some newlines and description only if there is a diff
                    diff_str = '\n\n# ' + diff_str
                initial_message += diff_str
                with tempfile.NamedTemporaryFile(suffix=".tmp") as tf:
                    tf.write(bytes(initial_message, 'utf-8'))
                    tf.flush()
                    call([EDITOR, tf.name])
                    with open(tf.name) as tf2:
                        message_lines = [l for l in tf2.readlines()
                                         if not l.startswith('#') and len(l.strip()) > 0]
                        message = '\n'.join(message_lines)

            if len(message) == 0:
                self.print_err('Aborting: Commit message is empty')
                raise RuntimeError

            params = self.read_global_params()
            commit_metadata = {
                'message': message,
                'author': params.get('author', None),
                'timestamp': datetime.utcnow().isoformat(),
                'is_merge': is_merge,
                'is_replay': is_replay
            }

        mfs_head = self.get_mfs_path(self.fs_repo_root, self.active_branch, branch_info='head')
        mfs_stage = self.get_mfs_path(self.fs_repo_root, self.active_branch, branch_info='stage')
        head_hash = self.ipfs.files_stat(mfs_head)['Hash']
        stage_hash = self.ipfs.files_stat(mfs_stage)['Hash']
        if head_hash == stage_hash:
            self.print_err('Nothing to commit')
            raise RuntimeError

        # Set head to stage
        try:
            self.ipfs.files_rm(mfs_head, recursive=True)
        except ipfsapi.exceptions.StatusError:
            pass

        self.ipfs.files_cp(mfs_stage, mfs_head)

        # Add parent pointer to previous head
        self.ipfs.files_cp(f'/ipfs/{head_hash}', f'{mfs_head}/parent')

        if is_merge and not is_replay:
            # Add merge_parent to merged head if this was a merge commit
            # and remove backups
            self.ipfs.files_cp(mfs_merge_parent, f'{mfs_head}/merge_parent')

            for ref in ['parent', 'head', 'stage', 'workspace']:
                p = self.get_mfs_path(
                    self.fs_repo_root, self.active_branch,
                    branch_info=f'merge_{ref}')
                self.ipfs.files_rm(p, recursive=True)
        elif is_replay:
            # We do nothing, we keep the backup references since we need them
            # to resume the replay
            pass

        # Add commit metadata
        metadata_bytes = io.BytesIO(json.dumps(commit_metadata).encode('utf-8'))
        self.ipfs.files_write(
            f'{mfs_head}/commit_metadata', metadata_bytes, create=True, truncate=True)

        return self.ipfs.files_stat(mfs_head)['Hash']

    @atomic
    def uncommit(self):
        # What to do with workspace changes?
        # Ask whether to overwrite or not?
        pass

    @atomic
    def diff(self):
        """ Content diff from head to stage """
        self.common()
        self._notify_pull_merge(self.fs_repo_root, self.active_branch)
        changes = self._diff_changes(Path('@stage'), Path('@head'))
        self.print(self._format_changes(changes, files=False))
        return changes
