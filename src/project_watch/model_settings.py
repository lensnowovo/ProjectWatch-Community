"""Classic compatibility adapter: keep the original default DSH configuration API."""
from project_watch.dsh_executor import DshProposalModel
from project_watch.model_configuration import ModelConfiguration
from project_watch.model_configuration import ModelInput as ModelInput
from project_watch.model_configuration import attach_model_settings as attach_model_settings


class ModelSettings(ModelConfiguration):
    """Existing callers retain DSH without making generic configuration depend on it."""

    def __init__(self, model=None):
        super().__init__(model, model_factory=DshProposalModel, executor_name="DSH")
