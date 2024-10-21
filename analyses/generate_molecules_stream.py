"""Generates molecules from a trained model."""

from typing import Sequence, Tuple, Iterable, Optional, Union, Callable

import os
import time

from absl import flags
from absl import app
from absl import logging
import ase
import ase.data
from ase.db import connect
import ase.io
import ase.visualize
import chex
import jax
import jax.numpy as jnp
import jraph
import numpy as np
import optax

import analyses.analysis as analysis
from symphony import datatypes
from symphony.data import input_pipeline
from symphony import models

FLAGS = flags.FLAGS

import os
import queue
import shutil

import jax
import jax.numpy as jnp
import numpy as np
import matplotlib.pyplot as plt
import jraph
import ase

from analyses import analysis
from symphony.data import input_pipeline
from symphony import datatypes
from symphony.models.utils import utils


def append_predictions(
    fragments: datatypes.Fragments, preds: datatypes.Predictions, radial_cutoff: float, max_len: int
) -> Iterable[Tuple[int, datatypes.Fragments]]:
    """Appends the predictions to the fragments."""
    # Bring back to CPU.
    fragments = jax.tree_util.tree_map(np.asarray, fragments)
    preds = jax.tree_util.tree_map(np.asarray, preds)
    valids = jraph.get_graph_padding_mask(fragments)

    # Process each fragment.
    for valid, fragment, pred in zip(
        valids, jraph.unbatch(fragments), jraph.unbatch(preds)
    ):
        if valid:
            yield *append_predictions_to_fragment(
                fragment, pred, max_len, radial_cutoff, 1e-4
            ), fragment, pred


def append_predictions_to_fragment(
    fragment: datatypes.Fragments,
    pred: datatypes.Predictions,
    max_len: int,
    radial_cutoff: float,
    eps: float,
) -> Tuple[int, datatypes.Fragments]:
    """Appends the predictions to a single fragment."""
    target_relative_positions = pred.globals.position_vectors[0]
    focus_indices = pred.globals.focus_indices[0]
    focus_positions = fragment.nodes.positions[focus_indices]
    extra_positions = (target_relative_positions + focus_positions).reshape(-1, 3)
    extra_species = (pred.globals.target_species[0]).reshape(-1,)
    stop = pred.globals.stop

    filtered_positions = []
    position_mask = np.ones(len(extra_positions), dtype=bool)
    for i in range(len(extra_positions)):
        if not position_mask[i]:
            continue
        if eps > np.linalg.norm(extra_positions[i]) > 0:
            position_mask[i] = False
        else:
            filtered_positions.append(extra_positions[i])
    extra_positions = np.array(filtered_positions)
    extra_species = extra_species[position_mask]
    extra_len = min(len(extra_positions), max_len)

    new_positions = np.concatenate([fragment.nodes.positions, extra_positions[:extra_len]], axis=0)
    new_species = np.concatenate([fragment.nodes.species, extra_species[:extra_len]], axis=0)

    atomic_numbers = np.asarray([1, 6, 7, 8, 9])
    new_fragment = input_pipeline.ase_atoms_to_jraph_graph(
        atoms=ase.Atoms(numbers=atomic_numbers[new_species], positions=new_positions),
        atomic_numbers=atomic_numbers,
        radial_cutoff=radial_cutoff,
    )
    new_fragment = new_fragment._replace(globals=fragment.globals)
    return stop, new_fragment


def _make_queue_iterator(q: queue.SimpleQueue):
    """Makes a non-blocking iterator from a queue."""
    while q.qsize() > 0:
        yield q.get(block=False)


def generate_molecules(
    apply_fn: Callable[[datatypes.Fragments, chex.PRNGKey], datatypes.Predictions],
    params: optax.Params,
    molecules_outputdir: str,
    radial_cutoff: float,
    focus_and_atom_type_inverse_temperature: float,
    position_inverse_temperature: float,
    num_seeds: int,
    num_seeds_per_chunk: int,
    init_molecules: Sequence[Union[str, ase.Atoms]],
    dataset: str,
    padding_mode: str,
    verbose: bool = False,
):
    """Generates molecules from a trained model at the given workdir."""
    logging.info("JAX host: %d / %d", jax.process_index(), jax.process_count())
    logging.info("JAX local devices: %r", jax.local_devices())
    logging.info("CUDA_VISIBLE_DEVICES: %r", os.environ.get("CUDA_VISIBLE_DEVICES"))

    # Create initial molecule, if provided.
    if isinstance(init_molecules, str):
        init_molecule, init_molecule_name = analysis.construct_molecule(init_molecules)
        logging.info(
            f"Initial molecule: {init_molecule.get_chemical_formula()} with numbers {init_molecule.numbers} and positions {init_molecule.positions}"
        )
        init_molecules = [init_molecule] * num_seeds
        init_molecule_names = [init_molecule_name] * num_seeds
    else:
        assert len(init_molecules) == num_seeds
        init_molecule_names = [
            init_molecule.get_chemical_formula() for init_molecule in init_molecules
        ]

    os.makedirs(molecules_outputdir, exist_ok=True)

    init_fragments = [
        input_pipeline.ase_atoms_to_jraph_graph(
            init_molecule, np.array([1, 6, 7, 8, 9]), radial_cutoff
        )
        for init_molecule in init_molecules
    ]

    fragment_pool = queue.SimpleQueue()
    for seed, init_fragment in enumerate(init_fragments):
        init_fragment = init_fragment._replace(
            globals=np.asarray([seed], dtype=np.int32)
        )
        fragment_pool.put(init_fragment)

    if dataset == "qm9":
        max_num_atoms = 35
    elif dataset == "tmqm":
        max_num_atoms = 60
    else:
        raise ValueError(f"Unknown dataset: {dataset}")
    padding_budget = dict(
        n_node=max_num_atoms,
        n_edge=max_num_atoms * 10,
        n_graph=2,
    )

    # Generate molecules.
    # Start timer to measure generation time.
    start_time = time.time()

    rng = jax.random.PRNGKey(0)
    generated_molecules = []
    while len(generated_molecules) < num_seeds and fragment_pool.qsize() > 0:
        logging.info(
            f"Fragment pool has {fragment_pool.qsize()} remaining fragments. "
            f"Generated {len(generated_molecules)} molecules so far."
        )

        for fragments in jraph.dynamically_batch(
            _make_queue_iterator(fragment_pool), **padding_budget
        ):
            # Compute predictions.
            apply_rng, rng = jax.random.split(rng)
            preds = apply_fn(
                params,
                apply_rng,
                fragments,
                focus_and_atom_type_inverse_temperature,
                position_inverse_temperature,
            )
            print("Computed all predictions.")

            for stop, new_fragment, fragment, pred in append_predictions(
                fragments, preds, radial_cutoff=radial_cutoff, max_len=max_num_atoms
            ):
                num_atoms_in_fragment = len(new_fragment.nodes.species)
                if stop or num_atoms_in_fragment >= max_num_atoms:
                    generated_molecules.append((stop, new_fragment))
                else:
                    fragment_pool.put(new_fragment)
            print("Appended all predictions.")



    # Add the remaining fragments to the generated molecules.
    while fragment_pool.qsize() > 0:
        print("Adding unfinished fragment to generated molecules.")
        unfinished_fragment = fragment_pool.get(block=False)
        print(f"Seed {unfinished_fragment.globals.item()} unfinished.")
        generated_molecules.append((False, unfinished_fragment))

    # Stop timer.
    elapsed_time = time.time() - start_time

    # Log generation time.
    logging.info(
        f"Generated {len(generated_molecules)} molecules in {elapsed_time} seconds."
    )
    logging.info(
        f"Average time per molecule: {elapsed_time / len(generated_molecules)} seconds."
    )

    generated_molecules_ase = []
    for stop, fragment in generated_molecules:
        seed = fragment.globals.item()
        print(f"Seed {seed} produced {fragment.n_node} atoms.")
        init_molecule_name = init_molecule_names[seed]
        generated_molecule_ase = ase.Atoms(
            symbols=utils.get_atomic_numbers(fragment.nodes.species),
            positions=fragment.nodes.positions,
        )

        if stop:
            logging.info("Generated %s", generated_molecule_ase.get_chemical_formula())
            output_file = f"{init_molecule_name}_seed={seed}.xyz"
        else:
            logging.info("STOP was not produced ...")
            output_file = f"{init_molecule_name}_seed={seed}_no_stop.xyz"

        generated_molecule_ase.write(os.path.join(molecules_outputdir, output_file))
        generated_molecules_ase.append(generated_molecule_ase)

    # Save the generated molecules as an ASE database.
    output_db = os.path.join(
        molecules_outputdir, f"generated_molecules_init={init_molecule_name}.db"
    )
    with connect(output_db) as conn:
        for mol in generated_molecules_ase:
            conn.write(mol)


def main(unused_argv: Sequence[str]) -> None:
    del unused_argv

    workdir = os.path.abspath(FLAGS.workdir)
    generate_molecules(
        workdir,
        FLAGS.outputdir,
        FLAGS.focus_and_atom_type_inverse_temperature,
        FLAGS.position_inverse_temperature,
        FLAGS.step,
        FLAGS.num_seeds,
        FLAGS.init,
        FLAGS.max_num_atoms,
        FLAGS.num_node_for_padding,
        FLAGS.num_edge_for_padding,
        FLAGS.num_graph_for_padding,
        FLAGS.steps_for_weight_averaging,
    )


if __name__ == "__main__":
    flags.DEFINE_string("workdir", None, "Workdir for model.")
    flags.DEFINE_string(
        "outputdir",
        os.path.join(os.getcwd(), "analyses", "analysed_workdirs"),
        "Directory where molecules should be saved.",
    )
    flags.DEFINE_float(
        "focus_and_atom_type_inverse_temperature",
        1.0,
        "Inverse temperature value for sampling the focus and atom type.",
        short_name="fait",
    )
    flags.DEFINE_float(
        "position_inverse_temperature",
        1.0,
        "Inverse temperature value for sampling the position.",
        short_name="pit",
    )
    flags.DEFINE_string(
        "step",
        "best",
        "Step number to load model from. The default corresponds to the best model.",
    )
    flags.DEFINE_integer(
        "num_seeds",
        1,
        "Seeds to attempt to generate molecules from.",
    )
    flags.DEFINE_string(
        "init",
        "C",
        "An initial molecular fragment to start the generation process from.",
    )
    flags.DEFINE_integer(
        "max_num_atoms",
        30,
        "Maximum number of atoms to generate per molecule.",
    )
    flags.DEFINE_integer(
        "num_node_for_padding",
        240,
        "Number of nodes to pad to.",
    )
    flags.DEFINE_integer(
        "num_edge_for_padding",
        900,
        "Number of edges to pad to.",
    )
    flags.DEFINE_integer(
        "num_graph_for_padding",
        8,
        "Number of graphs to pad to.",
    )
    flags.DEFINE_list(
        "steps_for_weight_averaging",
        None,
        "Steps to average parameters over. If None, the model at the given step is used.",
    )
    flags.mark_flags_as_required(["workdir"])
    app.run(main)
