import subprocess
import os
from datetime import datetime


class GitManagerAlreadyInitialized(Exception):
    pass

class GitNotAvailable(Exception):
    pass

class NotAGitRepositoryException(Exception):
    pass

class GitManager:
    __instance: "GitManager | None" = None

    @classmethod
    def get_instance(cls, working_dir: str | None = None) -> "GitManager":
        if cls.__instance is None:
            cls.__instance = cls(working_dir=working_dir)
        return cls.__instance

    def __init__(self, working_dir: str | None = None):
        if GitManager.__instance is not None:
            raise GitManagerAlreadyInitialized("GitManager is already initialized")
        self._working_dir = working_dir or os.getenv("WORKING_DIR") or os.getcwd()
        self._internal_confirm_git_present()
        GitManager.__instance = self

    def set_cwd(self, value: str) -> None:
        self._working_dir = value
        print("New working directory: %s" % self._working_dir)

    def git_version(self) -> int:
        return self._internal_process("-v").returncode

    def _internal_confirm_git_present(self):
        code = self._internal_process("-v").returncode
        if code != 0:
            raise GitNotAvailable("Git is not available to this utility")

    def _internal_process(self, *args: str) -> subprocess.CompletedProcess:
            process = subprocess.run(
            ["git", *args],
            cwd=self._working_dir,
            check=False,
             text=True,
            capture_output=True,
            )
            if process.returncode == 128:
                raise NotAGitRepositoryException(
                    f"The current working directory {self._working_dir} is not a git repository")
            return process

    def git_status(self, args : list[str]) -> subprocess.CompletedProcess:
        if not args:
            args = []
        args_str = " ".join(args)
        return self._internal_process("status", args_str)

    def has_untracked_files(self) -> bool:
        return len(self.untracked_files()) > 0

    def has_modified_files(self) -> bool:
        return len(self.modified_files()) > 0

    def has_deleted_files(self) -> bool:
        return len(self.deleted_files()) > 0

    def has_added_files(self) -> bool:
        return len(self.added_files()) > 0

    def untracked_files(self) -> list[str]:
        return self.files_of_git_type('?')

    def modified_files(self) -> list[str]:
        return self.files_of_git_type('M')

    def deleted_files(self) -> list[str]:
        return self.files_of_git_type('D')

    def added_files(self) -> list[str]:
        return self.files_of_git_type('A')

    def files_of_git_type(self, git_hub_code: str):
        """
        Returns files in the working directory that match a given git type
        'M' for modified files
        '?' for untracked files
        'D' for deleted files
        'A' for added files
        'R' for renamed files
        'T' for type changes files
        'C' for copied files
        :param git_hub_code: either '??', 'M' or 'D'
        :return: list of files that match the input type
        """
        curr_status = self.git_status(["--porcelain"])
        if curr_status.returncode != 0:
            return []

        output = str(curr_status.stdout).splitlines()
        files_of_type = []
        for line in output:
            if line[1] == git_hub_code:
                files_of_type.append(line[2:].strip())
        return files_of_type

    def cwd_has_changes(self) -> bool:
        curr_status = self.git_status(["--porcelain"])
        return curr_status.returncode == 0 and bool(curr_status.stdout.strip())

    def git_rev_list(self, args: list[str]) -> list[str]:
        git_status = self._internal_process("rev-list", *args)
        return git_status.stdout.splitlines()

    def cwd_has_unstaged_changes(self) -> bool:
        commit_count_local = int(*self.git_rev_list(["--count", "HEAD"]))
        commit_count_remote = int(*self.git_rev_list(["--count", "ORIGIN"]))
        return commit_count_local > commit_count_remote

    def git_init(self) -> int:
        return self._internal_process("init").returncode

    def git_add(self, files: list[str] | None = None) -> None:
        if not files:
            files = ["."]
        self._internal_process("add", *files)

    def git_commit(self, message: str | None = None) -> int:
        msg = message or self._generate_commit_message()
        return self._internal_process("commit", "-m", msg).returncode

    def git_push(self, remote: str = "origin", branch: str = "main") -> None:
        self._internal_process("push", remote, branch)

    def git_pull(self, remote: str = "origin", branch: str = "main") -> None:
        self._internal_process("pull", remote, branch)

    def git_rev_parse(self, revision: str = "HEAD") -> tuple[int, str | None]:
        process_info = self._internal_process("rev-parse", revision)
        return process_info.returncode, str(process_info.stdout).strip()

    def _generate_commit_message(self) -> str:
        dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return "Automated deployment commit on %s" % dt_str

    def git_branch(self, branch_name: str) -> None:
        self._internal_process(" branch", "-M", branch_name)

    def git_tag(self, tag_name: str) -> None:
        self._internal_process("tag", tag_name)