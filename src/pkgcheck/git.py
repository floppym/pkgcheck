"""Git specific support and addon."""

import argparse
import os
import pickle
import re
import shlex
import subprocess
from contextlib import AbstractContextManager
from functools import partial

from pathspec import PathSpec
from pkgcore.ebuild import cpv
from pkgcore.ebuild.atom import MalformedAtom
from pkgcore.ebuild.atom import atom as atom_cls
from pkgcore.repository import multiplex
from pkgcore.repository.util import SimpleTree
from pkgcore.restrictions import packages, values
from snakeoil.cli import arghparse
from snakeoil.cli.exceptions import UserException
from snakeoil.compatibility import IGNORED_EXCEPTIONS
from snakeoil.fileutils import AtomicWriteFile
from snakeoil.iterables import partition
from snakeoil.klass import jit_attr
from snakeoil.osutils import pjoin
from snakeoil.process import CommandNotFound, find_binary
from snakeoil.strings import pluralism

from . import base, caches, objects
from .checks import GitCheck
from .eclass import matching_eclass
from .log import logger


class GitCommit:
    """Git commit objects."""

    def __init__(self, hash, commit_date, author, committer, message):
        self.hash = hash
        self.commit_date = commit_date
        self.author = author
        self.committer = committer
        self.message = message

    def __str__(self):
        return self.hash

    def __eq__(self, other):
        return self.hash == other.hash


class GitPkgChange:
    """Git package change objects."""

    def __init__(self, atom, status, commit, **kwargs):
        self.atom = atom
        self.status = status
        self.commit = commit
        self.data = kwargs


class GitError(Exception):
    """Generic git-related error."""


class GitCache(caches.DictCache):
    """Dictionary-based cache that encapsulates git commit data."""

    def __init__(self, *args, commit):
        super().__init__(*args)
        self.commit = commit


class ParsedGitRepo:
    """Parse repository git logs."""

    # git command to run on the targeted repo
    _git_cmd = 'git log --name-status --date=short --diff-filter=ARMD'

    # hacky path regexes for git log parsing, proper validation is handled later
    _ebuild_regex = '([^/]+)/[^/]+/([^/]+)\\.ebuild'
    _git_log_regex = re.compile(
        fr'^([ADM])\t{_ebuild_regex}|(R)\d+\t{_ebuild_regex}\t{_ebuild_regex}$')

    def __init__(self, path):
        self.path = path

    def update(self, commit_range, data=None, local=False, **kwargs):
        """Generate git commit data starting at a given commit hash."""
        if data is None:
            data = {}
        seen = set()
        for pkg in self.parse_git_log(commit_range, pkgs=True, local=local, **kwargs):
            atom = pkg.atom
            key = (atom, pkg.status)
            if key not in seen:
                seen.add(key)
                if local:
                    commit = (atom.fullver, pkg.commit.commit_date, pkg.commit, pkg.data)
                else:
                    commit = (atom.fullver, pkg.commit.commit_date, pkg.commit.hash)
                data.setdefault(atom.category, {}).setdefault(
                    atom.package, {}).setdefault(pkg.status, []).append(commit)
        return data

    def parse_git_log(self, commit_range, pkgs=False, local=False, verbosity=-1):
        """Parse git log output."""
        cmd = shlex.split(self._git_cmd)
        # custom git log format, see the "PRETTY FORMATS" section of the git
        # log man page for details
        format_lines = [
            '# BEGIN COMMIT',
            '%h',  # abbreviated commit hash
            '%cd',  # commit date
            '%an <%ae>',  # Author Name <author@email.com>
            '%cn <%ce>',  # Committer Name <committer@email.com>
            '%B',  # commit message
            '# END MESSAGE BODY',
        ]
        format_str = '%n'.join(format_lines)
        cmd.append(f'--pretty=tformat:{format_str}')
        cmd.append(commit_range)

        git_log = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.path)
        line = git_log.stdout.readline().decode().strip()
        if git_log.poll():
            error = git_log.stderr.read().decode().strip()
            logger.warning('skipping git checks: %s', error)
            return

        count = 1
        with base.ProgressManager(verbosity=verbosity) as progress:
            while line:
                hash = git_log.stdout.readline().decode().strip()
                commit_date = git_log.stdout.readline().decode().strip()
                author = git_log.stdout.readline().decode('utf-8', 'replace').strip()
                committer = git_log.stdout.readline().decode('utf-8', 'replace').strip()

                message = []
                while True:
                    line = git_log.stdout.readline().decode('utf-8', 'replace').strip('\n')
                    if line == '# END MESSAGE BODY':
                        # drop trailing newline if it exists
                        if not message[-1]:
                            message.pop()
                        break
                    message.append(line)

                # update progress output
                progress(f'updating git cache: commit #{count}, {commit_date}')
                count += 1

                commit = GitCommit(hash, commit_date, author, committer, message)
                if not pkgs:
                    yield commit

                # file changes
                while True:
                    line = git_log.stdout.readline().decode('utf-8', 'replace')
                    if line == '# BEGIN COMMIT\n' or not line:
                        break
                    line = line.strip()
                    if pkgs and line:
                        match = self._git_log_regex.match(line)
                        if match is not None:
                            data = match.groups()
                            try:
                                if data[0] is not None:
                                    # matched ADM status change
                                    status, category, pkg = data[0:3]
                                    yield GitPkgChange(
                                        atom_cls(f'={category}/{pkg}'), status, commit)
                                else:
                                    # matched R status change
                                    status, category, pkg = data[3:6]
                                    old_atom = atom_cls(f'={category}/{pkg}')
                                    yield GitPkgChange(old_atom, status, commit)
                                    # include old, renamed pkg for local commits repo
                                    if local:
                                        category, pkg = data[6:]
                                        yield GitPkgChange(
                                            atom_cls(f'={category}/{pkg}'),
                                            status, commit, old_atom=old_atom)
                            except MalformedAtom:
                                pass


class _GitCommitPkg(cpv.VersionedCPV):
    """Fake packages encapsulating commits parsed from git log."""

    def __init__(self, category, package, status, version, date, commit, data=None):
        super().__init__(category, package, version)

        # add additional attrs
        sf = object.__setattr__
        sf(self, 'date', date)
        sf(self, 'status', status)
        sf(self, 'commit', commit)
        if data is not None:
            for k, v in data.items():
                sf(self, k, v)


class GitChangedRepo(SimpleTree):
    """Historical git repo consisting of the latest changed packages."""

    # selected pkg status filter
    _status_filter = {'A', 'R', 'M', 'D'}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('pkg_klass', _GitCommitPkg)
        super().__init__(*args, **kwargs)

    def _get_versions(self, cp):
        versions = []
        for status, data in self.cpv_dict[cp[0]][cp[1]].items():
            if status in self._status_filter:
                versions.append((status, data))
        return versions

    def _internal_gen_candidates(self, candidates, sorter, raw_pkg_cls, **kwargs):
        for cp in sorter(candidates):
            yield from sorter(
                raw_pkg_cls(cp[0], cp[1], status, *commit)
                for status, data in self.versions.get(cp, ())
                for commit in data)


class GitModifiedRepo(GitChangedRepo):
    """Historical git repo consisting of the latest modified packages."""

    _status_filter = {'A', 'R', 'M'}


class GitAddedRepo(GitChangedRepo):
    """Historical git repo consisting of added packages."""

    _status_filter = {'A', 'R'}


class GitRemovedRepo(GitChangedRepo):
    """Historical git repo consisting of removed packages."""

    _status_filter = {'D'}


class _ScanCommits(argparse.Action):
    """Argparse action that enables git commit checks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def _pkg_atoms(paths):
        """Filter package atoms from commit paths."""
        for x in paths:
            try:
                yield atom_cls(os.sep.join(x.split(os.sep, 2)[:2]))
            except MalformedAtom:
                continue

    def __call__(self, parser, namespace, value, option_string=None):
        if namespace.targets:
            targets = ' '.join(namespace.targets)
            s = pluralism(namespace.targets)
            parser.error(f'--commits is mutually exclusive with target{s}: {targets}')

        namespace.forced_checks.extend(
            name for name, cls in objects.CHECKS.items() if issubclass(cls, GitCheck))
        ref = value if value is not None else 'origin'
        setattr(namespace, self.dest, ref)

        # generate restrictions based on git commit changes
        repo = namespace.target_repo
        targets = sorted(repo.category_dirs)
        if os.path.isdir(pjoin(repo.location, 'eclass')):
            targets.append('eclass')
        git_diff_cmd = ['git', 'diff', '--cached', ref, '--name-only']
        try:
            p = subprocess.run(
                git_diff_cmd + targets,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=repo.location, check=True, encoding='utf8')
        except FileNotFoundError:
            parser.error('git not available to determine targets for --commits')
        except subprocess.CalledProcessError as e:
            error = e.stderr.splitlines()[0]
            parser.error(f'failed running git: {error}')

        if not p.stdout:
            # no changes exist, exit early
            parser.exit()

        pkgs, eclasses = partition(
            p.stdout.splitlines(), predicate=lambda x: x.startswith('eclass/'))
        pkgs = sorted(self._pkg_atoms(pkgs))

        eclass_regex = re.compile(r'^eclass/(?P<eclass>\S+)\.eclass$')
        eclasses = filter(None, (eclass_regex.match(x) for x in eclasses))
        eclasses = sorted(x.group('eclass') for x in eclasses)

        restrictions = []
        if pkgs:
            restrict = packages.OrRestriction(*pkgs)
            restrictions.append((base.package_scope, restrict))
        if eclasses:
            func = partial(matching_eclass, frozenset(eclasses))
            restrict = values.AnyMatch(values.FunctionRestriction(func))
            restrictions.append((base.eclass_scope, restrict))

        # no pkgs or eclasses to check, exit early
        if not restrictions:
            parser.exit()

        namespace.contexts.append(GitStash(parser, repo))
        namespace.restrictions = restrictions


class GitStash(AbstractContextManager):
    """Context manager for stashing untracked or modified/uncommitted files.

    This assumes that no git actions are performed on the repo while a scan is
    underway otherwise `git stash` usage may cause issues.
    """

    def __init__(self, parser, repo):
        self.parser = parser
        self.repo = repo
        self._stashed = False

    def __enter__(self):
        """Stash all untracked or modified files in working tree."""
        # check for untracked or modified/uncommitted files
        p = subprocess.run(
            ['git', 'ls-files', '-mo', '--exclude-standard'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.repo.location, encoding='utf8')
        if p.returncode != 0 or not p.stdout:
            return

        # stash all existing untracked or modified/uncommitted files
        try:
            subprocess.run(
                ['git', 'stash', 'push', '-u', '-m', 'pkgcheck scan --commits'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                cwd=self.repo.location, check=True, encoding='utf8')
        except subprocess.CalledProcessError as e:
            error = e.stderr.splitlines()[0]
            self.parser.error(f'git failed stashing files: {error}')
        self._stashed = True

    def __exit__(self, _exc_type, _exc_value, _traceback):
        """Apply any previously stashed files back to the working tree."""
        if self._stashed:
            try:
                subprocess.run(
                    ['git', 'stash', 'pop'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    cwd=self.repo.location, check=True, encoding='utf8')
            except subprocess.CalledProcessError as e:
                error = e.stderr.splitlines()[0]
                self.parser.error(f'git failed applying stash: {error}')


class GitAddon(caches.CachedAddon):
    """Git repo support for various checks.

    Pkgcheck can create virtual package repos from a given git repo's history
    in order to provide more info for checks relating to stable requests,
    outdated blockers, or local commits. These virtual repos are cached and
    updated every run if new commits are detected.

    Git repos must have a supported config in order to work properly.
    Specifically, pkgcheck assumes that both origin and master branches exist
    and relate to the upstream and local development states, respectively.

    Additionally, the origin/HEAD ref must exist. If it doesn't, running ``git
    fetch origin`` should create it. Otherwise, using ``git remote set-head
    origin master`` or similar will also create the reference.
    """

    # cache registry
    cache = caches.CacheData(type='git', file='git.pickle', version=4)

    @classmethod
    def mangle_argparser(cls, parser):
        group = parser.add_argument_group('git', docs=cls.__doc__)
        group.add_argument(
            '--commits', action=arghparse.Delayed, target=_ScanCommits,
            nargs='?', metavar='COMMIT',
            help="determine scan targets from local git repo commits",
            docs="""
                For a local git repo, pkgcheck will determine targets to scan
                from the committed changes compared to a given reference that
                defaults to the repo's origin.

                For example, to scan all the packages that have been changed in
                the current branch compared to the branch named 'old' use
                ``pkgcheck scan --commits old``. For two separate branches
                named 'old' and 'new' use ``pkgcheck scan --commits old..new``.

                Note that will also enable eclass-specific checks if it
                determines any commits have been made to eclasses.
            """)

    def __init__(self, *args):
        super().__init__(*args)
        # disable git support if git isn't installed
        if self.options.cache['git']:
            try:
                find_binary('git')
            except CommandNotFound:
                self.options.cache['git'] = False

        # mapping of repo locations to their corresponding git repo caches
        self._cached_repos = {}

    @jit_attr
    def _gitignore(self):
        """Load a repo's .gitignore and .git/info/exclude files for path matching."""
        patterns = []
        for path in ('.gitignore', '.git/info/exclude'):
            try:
                with open(pjoin(self.options.target_repo.location, path)) as f:
                    patterns.extend(f)
            except FileNotFoundError:
                pass
            except IOError as e:
                logger.warning(f'failed reading {path!r}: {e}')
        return PathSpec.from_lines('gitwildmatch', patterns)

    def gitignored(self, path):
        """Determine if a given path in a repository is matched by .gitignore settings."""
        if path.startswith(self.options.target_repo.location):
            repo_prefix_len = len(self.options.target_repo.location) + 1
            path = path[repo_prefix_len:]
        return self._gitignore.match_file(path)

    @staticmethod
    def _get_commit_hash(path, commit='origin/HEAD'):
        """Retrieve a git repo's commit hash for a specific commit object."""
        try:
            p = subprocess.run(
                ['git', 'rev-parse', commit],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=path, check=True, encoding='utf8')
        except subprocess.CalledProcessError:
            raise GitError(f'failed retrieving commit hash for git repo: {path}')
        return p.stdout.strip()

    def update_cache(self, force=False):
        """Update related cache and push updates to disk."""
        if self.options.cache['git']:
            for repo in self.repos:
                try:
                    commit = self._get_commit_hash(repo.location)
                except GitError:
                    continue

                # initialize cache file location
                cache_file = self.cache_file(repo)

                git_cache = None
                cache_repo = True
                if not force:
                    # try loading cached, historical repo data
                    try:
                        with open(cache_file, 'rb') as f:
                            git_cache = pickle.load(f)
                        if git_cache.version != self.cache.version:
                            logger.debug('forcing git repo cache regen due to outdated version')
                            os.remove(cache_file)
                            git_cache = None
                    except IGNORED_EXCEPTIONS:
                        raise
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        logger.debug('forcing git repo cache regen: %s', e)
                        os.remove(cache_file)
                        git_cache = None

                if git_cache is None or commit != git_cache.commit:
                    logger.debug('updating %s git repo cache to %s', repo, commit[:13])
                    if git_cache is None:
                        data = {}
                        commit_range = 'origin/HEAD'
                    else:
                        data = git_cache.data
                        commit_range = f'{git_cache.commit}..origin/HEAD'
                    git_repo = ParsedGitRepo(repo.location)
                    git_repo.update(commit_range, data=data, verbosity=self.options.verbosity)
                    git_cache = GitCache(data, self.cache, commit=commit)
                else:
                    cache_repo = False

                if git_cache is not None:
                    self._cached_repos[repo.location] = git_cache
                    # push repo to disk if it was created or updated
                    if cache_repo:
                        try:
                            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                            with AtomicWriteFile(cache_file, binary=True) as f:
                                pickle.dump(git_cache, f, protocol=-1)
                        except IOError as e:
                            msg = f'failed dumping git pkg repo: {cache_file!r}: {e.strerror}'
                            raise UserException(msg)

    def cached_repo(self, repo_cls, target_repo=None):
        cached_repo = None
        if target_repo is None:
            target_repo = self.options.target_repo

        if self.options.cache['git']:
            git_repos = []
            for repo in target_repo.trees:
                git_cache = self._cached_repos.get(repo.location, None)
                # only enable repo queries if history was found, e.g. a
                # shallow clone with a depth of 1 won't have any history
                if git_cache:
                    git_repos.append(repo_cls(git_cache, repo_id=f'{repo.repo_id}-history'))
                else:
                    logger.warning('skipping git checks for %s repo', repo)
                    break
            else:
                if len(git_repos) > 1:
                    cached_repo = multiplex.tree(*git_repos)
                elif len(git_repos) == 1:
                    cached_repo = git_repos[0]

        return cached_repo

    def commits_repo(self, repo_cls, target_repo=None, options=None):
        options = options if options is not None else self.options
        if target_repo is None:
            target_repo = options.target_repo

        commits = {}
        repo_id = f'{target_repo.repo_id}-commits'

        if options.cache['git']:
            try:
                origin = self._get_commit_hash(target_repo.location)
                master = self._get_commit_hash(target_repo.location, commit='master')
                if origin != master:
                    git_repo = ParsedGitRepo(target_repo.location)
                    commits = git_repo.update('origin/HEAD..master', local=True)
            except GitError as e:
                logger.warning('skipping git commit checks: %s', e)

        return repo_cls(commits, repo_id=repo_id)

    def commits(self, repo=None):
        path = repo.location if repo is not None else self.options.target_repo.location
        commits = iter(())

        if self.options.cache['git']:
            try:
                origin = self._get_commit_hash(path)
                master = self._get_commit_hash(path, commit='master')
                if origin != master:
                    git_repo = ParsedGitRepo(path)
                    commits = git_repo.parse_git_log('origin/HEAD..master')
            except GitError as e:
                logger.warning('skipping git commit checks: %s', e)

        return commits
