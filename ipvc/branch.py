import io
import os
import sys
import json
import webbrowser
from pathlib import Path

import ipfsapi
from ipvc.common import CommonAPI, expand_ref, refpath_to_mfs, make_len, atomic

class BranchAPI(CommonAPI):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @atomic
    def status(self, name=False):
        _, branch = self.common()
        active = self.ipfs.files_read(
            self.get_mfs_path(self.fs_cwd, repo_info='active_branch_name')).decode('utf-8')
        if not self.quiet: print(active)
        return active

    @atomic
    def create(self, name, from_commit="@head", no_checkout=False):
        _, branch = self.common()

        if not name.replace('_', '').isalnum():
            if not self.quiet:
                print('Branch name has to be alpha numeric with underscores',
                      file=sys.stderr)
            raise RuntimeError()
        elif name in ['head', 'workspace', 'stage']:
            if not self.quiet:
                print(f'"{name}" is a reserved keyword, please pick a different branch name',
                      file=sys.stderr)
            raise RuntimeError()


        try:
            self.ipfs.files_stat(self.get_mfs_path(self.fs_cwd, name))
            if not self.quiet: print('Branch name already exists', file=sys.stderr)
            raise RuntimeError()
        except ipfsapi.exceptions.StatusError:
            pass

        if from_commit == "@head":
            # Simply copy the current branch to the new branch
            self.ipfs.files_cp(
                self.get_mfs_path(self.fs_cwd, branch),
                self.get_mfs_path(self.fs_cwd, name))
        else:
            # Create the branch directory along with an empty stage and workspace
            for ref in ['stage', 'workspace']:
                mfs_ref = self.get_mfs_path(self.fs_cwd, name, branch_info=ref)
                self.ipfs.files_mkdir(mfs_ref, parents=True)

            # Copy the commit to the new branch's head
            commit_path = expand_ref(from_commit)
            mfs_commit_path = self.get_mfs_path(
                self.fs_cwd, branch, branch_info=commit_path)
            mfs_head_path = self.get_mfs_path(
                self.fs_cwd, name, branch_info='head')

            try:
                self.ipfs.files_stat(mfs_commit_path)
            except ipfsapi.exceptions.StatusError:
                if not self.quiet:
                    print('No such commit', file=sys.stderr)
                raise RuntimeError()

            self.ipfs.files_cp(mfs_commit_path, mfs_head_path)

            # Copy commit bundle to workspace and stage, plus a parent1 link
            # from stage to head
            mfs_commit_bundle_path = f'{mfs_commit_path}/bundle'
            mfs_workspace_path = self.get_mfs_path(
                self.fs_cwd, name, branch_info='workspace/bundle')
            mfs_stage_path = self.get_mfs_path(
                self.fs_cwd, name, branch_info='stage/bundle')
            self.ipfs.files_cp(mfs_commit_bundle_path, mfs_workspace_path)
            self.ipfs.files_cp(mfs_commit_bundle_path, mfs_stage_path)

        if not no_checkout:
            self.checkout(name)

    def _load_ref_into_repo(self, fs_repo_root, branch, ref,
                            without_timestamps=False):
        """ Syncs the fs workspace with the files in ref """
        metadata = self.read_metadata(ref)
        added, removed, modified = self.workspace_changes(
            fs_repo_root, metadata, update_meta=False)

        mfs_refpath, _ = refpath_to_mfs(Path(f'@{ref}'))

        for path in added:
            os.remove(path)

        for path in removed | modified:
            mfs_path = self.get_mfs_path(
                fs_repo_root, branch,
                branch_info=(mfs_refpath / path.relative_to(fs_repo_root)))

            timestamp = metadata[str(path)]['timestamp']

            with open(path, 'wb') as f:
                f.write(self.ipfs.files_read(mfs_path))

            os.utime(path, ns=(timestamp, timestamp))

    @atomic
    def checkout(self, name, without_timestamps=False):
        """ Checks out a branch"""
        fs_repo_root, _ = self.common()

        try:
            self.ipfs.files_stat(self.get_mfs_path(self.fs_cwd, name))
        except ipfsapi.exceptions.StatusError:
            if not self.quiet: print('No branch by that name exists', file=sys.stderr)
            raise RuntimeError()

        # Write the new branch name to active_branch_name
        # NOTE: truncate here is needed to clear the file before writing
        self.ipfs.files_write(
            self.get_mfs_path(self.fs_cwd, repo_info='active_branch_name'),
            io.BytesIO(bytes(name, 'utf-8')),
            create=True, truncate=True)

        self._load_ref_into_repo(
            fs_repo_root, name, 'workspace', without_timestamps)

    @atomic
    def history(self, show_hash=False):
        """ Shows the commit history for the current branch. Currently only shows
        the linear history on the first parents side"""
        fs_repo_root, branch = self.common()

        # Traverse the commits backwards by via the {commit}/parent1/ link
        mfs_commit_path = self.get_mfs_path(
            fs_repo_root, branch, branch_info=Path('head'))
        commit_hash = self.ipfs.files_stat(
            mfs_commit_path)['Hash']

        commits = []
        while True:
            commit_ref_hash = self.ipfs.files_stat(
                f'/ipfs/{commit_hash}/bundle/files')['Hash']
            try:
                meta = json.loads(self.ipfs.cat(f'/ipfs/{commit_hash}/metadata').decode('utf-8'))
            except ipfsapi.exceptions.StatusError:
                # Reached the root of the graph
                break

            h, ts, msg = commit_hash[:6], meta['timestamp'], meta['message']
            auth = make_len(meta['author'] or '', 30)
            if not self.quiet: 
                if show_hash:
                    print(f'* {commit_ref_hash} {ts} {auth}   {msg}')
                else:
                    print(f'* {ts} {auth}   {msg}')

            commits.append(commit_hash)

            try:
                commit_hash = self.ipfs.files_stat(f'/ipfs/{commit_hash}/parent1')['Hash']
            except ipfsapi.exceptions.StatusError:
                # Reached the root of the graph
                break

        return commits

    @atomic
    def show(self, refpath, browser=False):
        """ Opens a ref in the ipfs file browser """
        mfs_commit_hash = self.get_refpath_hash(refpath)
        if browser:
            # TODO: read IPFS node url from settings
            url = f'http://localhost:8080/ipfs/{mfs_commit_hash}'
            if not self.quiet: print(f'Opening {url}')
            webbrowser.open(url)
        else:
            ret = self.ipfs.ls(f'/ipfs/{mfs_commit_hash}')
            obj = ret['Objects'][0]
            if len(obj['Links']) == 0:
                # It's a file, so cat it
                cat = self.ipfs.cat(f'/ipfs/{mfs_commit_hash}').decode('utf-8')
                if not self.quiet:
                    print(cat)
                return cat
            else:
                # It's a folder
                ls = '\n'.join([ln['Name'] for ln in obj['Links']])
                if not self.quiet:
                    print(ls)
                return ls

    @atomic
    def merge(self, refpath):
        """ Merge refpath into this branch

        """
        pass

    @atomic
    def ls(self):
        """ List branches """
        fs_repo_root = self.get_repo_root()
        branches = self.get_branches(fs_repo_root)
        if not self.quiet:
            print('\n'.join(branches))
        return branches
