"""Defines the default hyperparameters and training configuration for the E3SchNet model."""

import ml_collections

from configs import default


def get_config() -> ml_collections.ConfigDict:
    """Get the hyperparameter configuration for the MACE model."""
    config = default.get_config()

    # Optimizer.
    config.optimizer = "adam"
    config.learning_rate = 1e-3

    # GNN hyperparameters.
    config.model = "E3SchNet"
    config.cutoff = 5.0
    config.num_interactions = 1
    config.num_basis_fns = 25
    config.num_channels = 128
    config.max_ell = 3
    config.activation = "shifted_softplus"

    return ml_collections.FrozenConfigDict(config)
