from side_effects.errors import SideEffectExecutionError


class GitHubWriteConfigError(Exception):
    def __init__(self, error_code: str = "github_write_config_invalid"):
        self.error_code = error_code
        super().__init__(error_code)


class GitHubAdapterError(SideEffectExecutionError):
    def __init__(self, error_code: str = "github_adapter_error"):
        super().__init__(error_code)
