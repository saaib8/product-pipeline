from django.apps import AppConfig


class PipelineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pipeline"

    def ready(self) -> None:
        # Importing a stage module runs its register() call. Without this the registry
        # is empty and `run_stage --all` silently does nothing.
        from pipeline.stages import icon_2d, ingest, metadata  # noqa: F401
