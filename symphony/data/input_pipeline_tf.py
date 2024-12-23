"""Input pipeline for the datasets with the tf.data API."""

import functools
from typing import Dict, List, Sequence, Tuple, Iterator, Optional
import re
import itertools
import os

from absl import logging
import tensorflow as tf
import chex
import jax
import numpy as np
import jraph
import ml_collections
import ase

from symphony.data import input_pipeline, fragments
from symphony import datatypes


def get_datasets(
    rng: chex.PRNGKey,
    config: ml_collections.ConfigDict,
) -> Dict[str, tf.data.Dataset]:
    """Loads and preprocesses the dataset as tf.data.Datasets for each split."""

    # Get the raw datasets.
    if config.dataset == "qm9":
        del rng
        datasets = get_unbatched_qm9_datasets(config)
    elif config.dataset == "tetris":
        datasets = get_unbatched_tetris_datasets(rng, config)
    elif config.dataset == "platonic_solids":
        datasets = get_unbatched_platonic_solids_datasets(rng, config)

    # Estimate the padding budget.
    if config.compute_padding_dynamically:
        max_n_nodes, max_n_edges, max_n_graphs = input_pipeline.estimate_padding_budget(
            datasets["train"], config.max_n_graphs, num_estimation_graphs=1000
        )

    else:
        max_n_nodes, max_n_edges, max_n_graphs = (
            config.max_n_nodes,
            config.max_n_edges,
            config.max_n_graphs,
        )

    logging.info(
        "Padding budget %s as: n_nodes = %d, n_edges = %d, n_graphs = %d",
        "computed" if config.compute_padding_dynamically else "provided",
        max_n_nodes,
        max_n_edges,
        max_n_graphs,
    )

    # Pad an example graph to see what the output shapes will be.
    # We will use this shape information when creating the tf.data.Dataset.
    example_graph = next(datasets["train"].as_numpy_iterator())
    example_padded_graph = jraph.pad_with_graphs(
        example_graph, n_node=max_n_nodes, n_edge=max_n_edges, n_graph=max_n_graphs
    )
    padded_graphs_spec = _specs_from_graphs_tuple(
        example_padded_graph, unknown_first_dimension=False
    )

    # Batch and pad each split separately.
    for split in ["train", "val", "test"]:
        dataset_split = datasets[split]

        # We repeat all splits indefinitely.
        # This is required because of some weird behavior of tf.data.Dataset.from_generator.
        dataset_split = dataset_split.repeat()

        # Now we batch and pad the graphs.
        batching_fn = functools.partial(
            jraph.dynamically_batch,
            graphs_tuple_iterator=iter(dataset_split),
            n_node=max_n_nodes,
            n_edge=max_n_edges,
            n_graph=max_n_graphs,
        )
        dataset_split = tf.data.Dataset.from_generator(
            batching_fn, output_signature=padded_graphs_spec
        )

        datasets[split] = dataset_split.prefetch(tf.data.AUTOTUNE).as_numpy_iterator()
        datasets[split + "_eval"] = (
            dataset_split.take(config.num_eval_steps).cache().prefetch(tf.data.AUTOTUNE)
        ).as_numpy_iterator()
        datasets[split + "_eval_final"] = (
            dataset_split.take(config.num_eval_steps_at_end_of_training)
            .cache()
            .prefetch(tf.data.AUTOTUNE)
        ).as_numpy_iterator()

    return datasets


def get_pieces_for_tetris() -> List[List[Tuple[int, int, int]]]:
    """Returns the pieces for Tetris."""
    # Taken from e3nn Tetris example.
    # https://docs.e3nn.org/en/stable/examples/tetris_gate.html
    return [
        [(0, 0, 0), (0, 0, 1), (1, 0, 0), (1, 1, 0)],  # chiral_shape_1
        [(0, 0, 0), (0, 0, 1), (1, 0, 0), (1, -1, 0)],  # chiral_shape_2
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],  # square
        [(0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 0, 3)],  # line
        [(0, 0, 0), (0, 0, 1), (0, 1, 0), (1, 0, 0)],  # corner
        [(0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 1, 0)],  # L
        [(0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 1, 1)],  # T
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (2, 1, 0)],  # zigzag
    ]


def get_unbatched_tetris_datasets(
    rng: chex.PRNGKey, config: ml_collections.ConfigDict
) -> Dict[str, tf.data.Dataset]:
    """Loads the raw Tetris dataset as a tf.data.Dataset for each split."""
    pieces = get_pieces_for_tetris()
    return pieces_to_unbatched_datasets(pieces, rng, config)


def get_unbatched_platonic_solids_datasets(
    rng: chex.PRNGKey, config: ml_collections.ConfigDict
) -> Dict[str, tf.data.Dataset]:
    """Loads the raw Platonic solids dataset as a tf.data.Dataset for each split."""
    pieces = get_pieces_for_platonic_solids()
    return pieces_to_unbatched_datasets(pieces, rng, config)


def pieces_to_unbatched_datasets(
    pieces: Sequence[Sequence[Tuple[int, int, int]]],
    rng: chex.PRNGKey,
    config: ml_collections.ConfigDict,
) -> Dict[str, tf.data.Dataset]:
    """Converts a sequence of pieces to a tf.data.Dataset for each split."""

    def generate_fragments_helper(
        rng: chex.PRNGKey, graph: jraph.GraphsTuple
    ) -> Iterator[datatypes.Fragments]:
        """Helper function to generate fragments from a graph."""
        return fragments.generate_fragments(
            rng,
            graph,
            n_species=1,
            nn_tolerance=config.nn_tolerance,
            max_radius=config.radial_cutoff,
            mode=config.fragment_logic,
        )

    # Convert to molecules, and then jraph.GraphsTuples.
    pieces_as_molecules = [
        ase.Atoms(numbers=np.asarray([1] * len(piece)), positions=np.asarray(piece))
        for piece in pieces
    ]
    pieces_as_graphs = [
        input_pipeline.ase_atoms_to_jraph_graph(
            molecule, [1], radial_cutoff=config.radial_cutoff
        )
        for molecule in pieces_as_molecules
    ]

    # Create an example graph to get the specs.
    # This is a bit ugly but I don't want to consume the generator.
    example_rng, rng = jax.random.split(rng)
    example_graph = next(
        iter(generate_fragments_helper(example_rng, pieces_as_graphs[0]))
    )
    element_spec = _specs_from_graphs_tuple(example_graph, unknown_first_dimension=True)

    # We will save our datasets to a temporary directory.
    datasets = {}

    for split in ["train", "val", "test"]:
        split_rng, rng = jax.random.split(rng)

        split_pieces = config.get(f"{split}_pieces")
        if None not in [split_pieces, split_pieces[0], split_pieces[1]]:
            split_pieces_as_graphs = pieces_as_graphs[split_pieces[0] : split_pieces[1]]
        else:
            split_pieces_as_graphs = pieces_as_graphs

        fragments_for_pieces = itertools.chain.from_iterable(
            generate_fragments_helper(split_rng, graph)
            for graph in split_pieces_as_graphs
        )

        def fragment_yielder():
            yield from fragments_for_pieces

        datasets[split] = tf.data.Dataset.from_generator(
            fragment_yielder, output_signature=element_spec
        )

        # This is a hack to get around the fact that tf.data.Dataset.from_generator
        # doesn't support looping. We just save and load the dataset to and from the disk.
        if not os.path.exists(f"{config.root_dir}/{os.getpid()}"):
            os.makedirs(f"{config.root_dir}/{os.getpid()}")
        dataset_path = f"{config.root_dir}/{os.getpid()}/{split}.tfrecord"
        datasets[split].save(dataset_path)
        datasets[split] = tf.data.Dataset.load(dataset_path, element_spec=element_spec)

        # Shuffle the dataset.
        if config.shuffle_datasets:
            datasets[split] = datasets[split].shuffle(1000, seed=0)

    return datasets


def _deprecated_get_unbatched_qm9_datasets(
    rng: chex.PRNGKey,
    root_dir: str,
    num_train_files: int,
    num_val_files: int,
    num_test_files: int,
) -> Dict[str, tf.data.Dataset]:
    """Loads the raw QM9 dataset as tf.data.Datasets for each split."""
    # Root directory of the dataset.
    filenames = os.listdir(root_dir)
    filenames = [os.path.join(root_dir, f) for f in filenames if "dataset_tf" in f]

    # Shuffle the filenames.
    shuffled_indices = jax.random.permutation(rng, len(filenames))
    shuffled_filenames = [filenames[i] for i in shuffled_indices]

    # Partition the filenames into train, val, and test.
    num_files_cumsum = np.cumsum([num_train_files, num_val_files, num_test_files])
    files_by_split = {
        "train": shuffled_filenames[: num_files_cumsum[0]],
        "val": shuffled_filenames[num_files_cumsum[0] : num_files_cumsum[1]],
        "test": shuffled_filenames[num_files_cumsum[1] : num_files_cumsum[2]],
    }

    element_spec = tf.data.Dataset.load(filenames[0]).element_spec
    datasets = {}
    for split, files_split in files_by_split.items():
        dataset_split = tf.data.Dataset.from_tensor_slices(files_split)
        dataset_split = dataset_split.interleave(
            lambda x: tf.data.Dataset.load(x, element_spec=element_spec),
            cycle_length=4,
            num_parallel_calls=tf.data.AUTOTUNE,
            deterministic=True,
        )

        datasets[split] = dataset_split
    return datasets


def get_unbatched_qm9_datasets(
    config: ml_collections.ConfigDict,
    seed: int = 0,
) -> Dict[str, tf.data.Dataset]:
    """Loads the raw QM9 dataset as tf.data.Datasets for each split."""
    # Set the seed for reproducibility.
    tf.random.set_seed(seed)

    # Root directory of the dataset.
    config = ml_collections.ConfigDict(config)
    config.root_dir = (
        "/Users/ameyad/Documents/spherical-harmonic-net/qm9_fragments_fixed/nn_edm"
    )
    config.train_molecules = (0, 100000)
    config.val_molecules = (100000, 110000)
    config.test_molecules = (110000, 130000)
    config.train_on_split_smaller_than_chunk = True

    filenames = sorted(os.listdir(config.root_dir))
    filenames = [
        os.path.join(config.root_dir, f)
        for f in filenames
        if f.startswith("fragments_")
    ]
    if len(filenames) == 0:
        raise ValueError(f"No files found in {config.root_dir}.")

    # Partition the filenames into train, val, and test.
    def filter_by_molecule_number(
        filenames: Sequence[str], start: int, end: int
    ) -> List[str]:
        def filter_file(filename: str, start: int, end: int) -> bool:
            filename = os.path.basename(filename)
            _, file_start, file_end = [int(val) for val in re.findall(r"\d+", filename)]
            return start <= file_start and file_end <= end

        return [f for f in filenames if filter_file(f, start, end)]

    # Number of molecules for training can be smaller than the chunk size.
    chunk_size = int(filenames[0].split("_")[-1])
    train_on_split_smaller_than_chunk = config.get("train_on_split_smaller_than_chunk")
    if train_on_split_smaller_than_chunk:
        train_molecules = (0, chunk_size)
    else:
        train_molecules = config.train_molecules
    files_by_split = {
        "train": filter_by_molecule_number(filenames, *train_molecules),
        "val": filter_by_molecule_number(filenames, *config.val_molecules),
        "test": filter_by_molecule_number(filenames, *config.test_molecules),
    }

    element_spec = tf.data.Dataset.load(filenames[0]).element_spec
    datasets = {}
    for split, files_split in files_by_split.items():
        if split == "train" and train_on_split_smaller_than_chunk:
            logging.info(
                "Training on a split of the training set smaller than a single chunk."
            )
            if config.train_molecules[1] >= chunk_size:
                raise ValueError(
                    "config.train_molecules[1] must be less than chunk_size if train_on_split_smaller_than_chunk is True."
                )

            dataset_split = tf.data.Dataset.load(files_split[0])
            num_molecules_seen = 0
            num_steps_to_take = None
            for step, molecule in enumerate(dataset_split):
                if molecule["n_node"][0] == 1:
                    if num_molecules_seen == config.train_molecules[0]:
                        num_steps_to_skip = step
                    if num_molecules_seen == config.train_molecules[1]:
                        num_steps_to_take = step - num_steps_to_skip
                        break
                    num_molecules_seen += 1

            if num_steps_to_take is None:
                raise ValueError(
                    "Could not find the correct number of molecules in the first chunk."
                )

            dataset_split = dataset_split.skip(num_steps_to_skip).take(
                num_steps_to_take
            )

        # This is usually the case, when the split is larger than a single chunk.
        else:
            dataset_split = tf.data.Dataset.from_tensor_slices(files_split)
            dataset_split = dataset_split.interleave(
                lambda path: tf.data.Dataset.load(path, element_spec=element_spec),
                num_parallel_calls=tf.data.AUTOTUNE,
                deterministic=True,
            )

        # Shuffle the dataset.
        if config.shuffle_datasets:
            dataset_split = dataset_split.shuffle(1000, seed=seed)

        # Convert to jraph.GraphsTuple.
        dataset_split = dataset_split.map(
            _convert_to_graphstuple,
            num_parallel_calls=tf.data.AUTOTUNE,
            deterministic=True,
        )

        datasets[split] = dataset_split
    return datasets


def _specs_from_graphs_tuple(
    graph: jraph.GraphsTuple, unknown_first_dimension: bool = False
):
    """Returns a tf.TensorSpec corresponding to this graph."""

    def get_tensor_spec(array: np.ndarray, is_global: bool = False) -> tf.TensorSpec:
        """Returns a tf.TensorSpec corresponding to this array."""
        shape = list(array.shape)
        if unknown_first_dimension and not is_global:
            shape = [None] + shape[1:]
        dtype = array.dtype
        return tf.TensorSpec(shape=shape, dtype=dtype)

    return jraph.GraphsTuple(
        nodes=datatypes.FragmentsNodes(
            positions=get_tensor_spec(graph.nodes.positions),
            species=get_tensor_spec(graph.nodes.species),
            focus_and_target_species_probs=get_tensor_spec(
                graph.nodes.focus_and_target_species_probs
            ),
        ),
        globals=datatypes.FragmentsGlobals(
            target_positions=get_tensor_spec(
                graph.globals.target_positions, is_global=True
            ),
            target_species=get_tensor_spec(
                graph.globals.target_species, is_global=True
            ),
            stop=get_tensor_spec(graph.globals.stop, is_global=True),
        ),
        edges=get_tensor_spec(graph.edges),
        receivers=get_tensor_spec(graph.receivers),
        senders=get_tensor_spec(graph.senders),
        n_node=get_tensor_spec(graph.n_node),
        n_edge=get_tensor_spec(graph.n_edge),
    )


def _convert_to_graphstuple(graph: Dict[str, tf.Tensor]) -> jraph.GraphsTuple:
    """Converts a dictionary of tf.Tensors to a GraphsTuple."""
    positions = graph["positions"]
    species = graph["species"]
    if "focus_and_target_species_probs" in graph:
        focus_and_target_species_probs = graph["focus_and_target_species_probs"]
    elif "target_species_probs" in graph:
        focus_and_target_species_probs = graph["target_species_probs"]
    else:
        raise ValueError(list(graph.keys()))

    stop = graph["stop"]
    receivers = graph["receivers"]
    senders = graph["senders"]
    n_node = graph["n_node"]
    n_edge = graph["n_edge"]
    edges = tf.ones((tf.shape(senders)[0], 1))
    target_positions = graph["target_positions"]
    target_species = graph["target_species"]

    return jraph.GraphsTuple(
        nodes=datatypes.FragmentsNodes(
            positions=positions,
            species=species,
            focus_and_target_species_probs=focus_and_target_species_probs,
        ),
        edges=edges,
        receivers=receivers,
        senders=senders,
        globals=datatypes.FragmentsGlobals(
            target_positions=target_positions,
            target_species=target_species,
            stop=stop,
        ),
        n_node=n_node,
        n_edge=n_edge,
    )
