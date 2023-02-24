"""Defines the default hyperparameters and training configuration for the MACE model."""

import ml_collections

from configs import default


def get_config() -> ml_collections.ConfigDict:
    """Get the hyperparameter configuration for the MACE model."""
    config = default.get_config()

    # Optimizer.
    config.optimizer = "adam"
    config.learning_rate = 5e-4

    # GNN hyperparameters.
    config.model = "HaikuMACE"
    config.species_embedding_dims = 32
    config.output_irreps = "128x0e"
    config.r_max = 5
    config.num_interactions = 1
    config.hidden_irreps = "128x0e + 128x1o + 128x2e"
    config.readout_mlp_irreps = "128x0e + 128x1o + 128x2e"
    config.avg_num_neighbors = 3
    config.num_species = 5
    config.max_ell = 2
    config.position_coeffs_lmax = 3
    return config
