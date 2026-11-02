"""Classic compatibility adapter: the original model configuration API, now a direct model call."""
from project_watch.model_configuration import ModelConfiguration
from project_watch.model_configuration import ModelInput as ModelInput
from project_watch.model_configuration import attach_model_settings as attach_model_settings
from project_watch.model_executor import DirectProposalModel


class ModelSettings(ModelConfiguration):
    """Existing callers retain DSH without making generic configuration depend on it."""

    def __init__(self, model=None):
        super().__init__(model, model_factory=DirectProposalModel, executor_name="direct")
