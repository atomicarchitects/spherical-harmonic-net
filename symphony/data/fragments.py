from typing import Iterator, Optional

import jax
import jraph
import numpy as np
import chex
from scipy.spatial.distance import pdist, squareform
import collections


from symphony import datatypes
from symphony.models import PeriodicTable


def generate_fragments(
    rng: chex.PRNGKey,
    graph: jraph.GraphsTuple,
    num_species: int,
    nn_tolerance: Optional[float],
    max_radius: Optional[float],
    mode: str,
    num_nodes_for_multifocus: int,
    heavy_first: bool,
    max_targets_per_graph: int,
    transition_first: bool,  # TODO currently only handles structures w 1 transition metal
) -> Iterator[datatypes.Fragments]:
    """Generative sequence for a molecular graph.

    Args:
        rng: The random number generator.
        graph: The molecular graph.
        num_species: The number of different species considered.
        nn_tolerance: Tolerance for the nearest neighbours.
        max_radius: The maximum distance of the focus-target
        mode: How to generate the fragments. Either "nn" or "radius".
        heavy_first: If true, the hydrogen atoms in the molecule will be placed last.
        max_targets_per_graph: The maximum number of targets per graph.

    Returns:
        A sequence of fragments.
    """
    if mode not in ["nn", "radius"]:
        raise ValueError("mode must be either 'nn' or 'radius'.")
    if mode == "radius" and max_radius is None:
        raise ValueError("max_radius must be specified for mode 'radius'.")
    if mode != "radius" and max_radius is not None:
        raise ValueError("max_radius specified, but mode is not 'radius'.")
    if mode == "nn" and nn_tolerance is None:
        raise ValueError("nn_tolerance must be specified for mode 'nn'.")
    if mode != "nn" and nn_tolerance is not None:
        raise ValueError("nn_tolerance specified, but mode is not 'nn'.")

    n = len(graph.nodes.positions)
    assert (
        len(graph.n_edge) == 1 and len(graph.n_node) == 1
    ), "Only single graphs supported."
    assert n >= 2, "Graph must have at least two nodes."

    # Compute edge distances.
    dist = np.linalg.norm(
        graph.nodes.positions[graph.receivers] - graph.nodes.positions[graph.senders],
        axis=1,
    )  # [n_edge]

    with jax.default_device(jax.devices("cpu")[0]):
        rng, visited_nodes, frag = _make_first_fragment(
            rng,
            graph,
            dist,
            num_species,
            nn_tolerance,
            max_radius,
            mode,
            num_nodes_for_multifocus,
            heavy_first,
            max_targets_per_graph,
            transition_first
        )
        yield frag

        counter = 0
        while counter < n - 2 and len(visited_nodes) < n:
            rng, visited_nodes, frag = _make_middle_fragment(
                rng,
                visited_nodes,
                graph,
                dist,
                num_species,
                nn_tolerance,
                max_radius,
                mode,
                num_nodes_for_multifocus,
                heavy_first,
                max_targets_per_graph,
            )
            yield frag
            counter += 1
        assert len(visited_nodes) == n
        yield _make_last_fragment(graph, num_species, max_targets_per_graph, num_nodes_for_multifocus)


def pick_targets(
    rng,
    targets,
    node_species,
    target_species_probability_for_focus,
    max_targets_per_graph,
):
    # Pick a random target species.
    rng, k = jax.random.split(rng)
    target_species = jax.random.choice(
        k,
        len(target_species_probability_for_focus),
        p=target_species_probability_for_focus,
    )

    # Pick up to max_targets_per_graph targets of the target species.
    targets_of_this_species = targets[node_species[targets] == target_species]
    targets_of_this_species = targets_of_this_species[:max_targets_per_graph]

    return targets_of_this_species


def _make_first_fragment(
    rng,
    graph,
    dist,
    num_species,
    nn_tolerance,
    max_radius,
    mode,
    num_nodes_for_multifocus,
    heavy_first,
    max_targets_per_graph,
    transition_first
):
    rng, k = jax.random.split(rng)
    if transition_first:
        ptable = PeriodicTable()
        bound1 = ptable.get_group(graph.nodes.species+1) >= 2
        bound2 = ptable.get_group(graph.nodes.species+1) <= 11
        transition_metals = (bound1 & bound2).astype(np.float32)
        transition_metals /= transition_metals.sum()
        first_node = jax.random.choice(
            k, np.arange(0, len(graph.nodes.positions)), p=transition_metals
        )
    elif heavy_first and (graph.nodes.species != 0).sum() > 0:
        heavy_indices = np.argwhere(graph.nodes.species != 0).squeeze(-1)
        first_node = jax.random.choice(k, heavy_indices)
    else:
        first_node = jax.random.choice(k, np.arange(0, len(graph.nodes.positions)))
    first_node = int(first_node)

    mask = graph.senders == first_node
    if heavy_first and (mask & graph.nodes.species[graph.receivers] > 0).sum() > 0:
        mask = mask & (graph.nodes.species[graph.receivers] > 0)
    if mode == "nn":
        min_dist = dist[mask].min()
        targets = graph.receivers[mask & (dist < min_dist + nn_tolerance)]
        del min_dist
    if mode == "radius":
        targets = graph.receivers[mask & (dist < max_radius)]

    if len(targets) == 0:
        raise ValueError("No targets found.")

    num_nodes = graph.nodes.positions.shape[0]
    target_species_probability = np.zeros((num_nodes, num_species))
    target_species_probability[first_node] = _normalized_bitcount(
        graph.nodes.species[targets], num_species
    )

    rng, k = jax.random.split(rng)
    target_nodes = pick_targets(
        k,
        targets,
        graph.nodes.species,
        target_species_probability[first_node],
        max_targets_per_graph,
    )
    target_mask = np.zeros((num_nodes_for_multifocus, max_targets_per_graph,))
    target_mask[0, : len(target_nodes)] = 1
    target_nodes = np.pad(target_nodes, (0, max_targets_per_graph - len(target_nodes)))

    sample = _into_fragment(
        graph,
        visited=np.array([first_node]),
        focus_mask=(np.arange(num_nodes) == first_node),
        target_species_probability=target_species_probability,
        target_nodes=np.expand_dims(target_nodes, axis=0),
        target_mask=target_mask,
        stop=False,
        max_targets_per_graph=max_targets_per_graph,
        num_nodes_for_multifocus=num_nodes_for_multifocus,
    )

    rng, k = jax.random.split(rng)
    next_node = jax.random.choice(k, target_nodes)
    visited = np.array([first_node, next_node])
    return rng, visited, sample


def _make_middle_fragment(
    rng,
    visited,
    graph,
    dist,
    num_species,
    nn_tolerance,
    max_radius,
    mode,
    num_nodes_for_multifocus,
    heavy_first,
    max_targets_per_graph,
):
    n_nodes = len(graph.nodes.positions)
    senders, receivers = graph.senders, graph.receivers

    mask = np.isin(senders, visited) & ~np.isin(receivers, visited)

    if heavy_first:
        heavy = graph.nodes.species > 0
        if heavy.sum() > heavy[visited].sum():
            mask = (
                mask
                & (graph.nodes.species[senders] > 0)
                & (graph.nodes.species[receivers] > 0)
            )

    if mode == "nn":
        min_dist = dist[mask].min()
        mask = mask & (dist < min_dist + nn_tolerance)
        del min_dist
    if mode == "radius":
        mask = mask & (dist < max_radius)

    # dists_masked = np.linalg.norm(graph.nodes.positions[receivers[mask]] - graph.nodes.positions[senders[mask]], axis=-1)
    # print("dists_masked", dists_masked)

    counts = np.zeros((n_nodes, num_species))
    for focus_node in range(n_nodes):
        targets = receivers[(senders == focus_node) & mask]
        counts[focus_node] = np.bincount(
            graph.nodes.species[targets], minlength=num_species
        )

    if np.sum(counts) == 0:
        raise ValueError("No targets found.")

    target_species_probability = counts / np.sum(counts)

    # pick random focus nodes
    focus_probability = _normalized_bitcount(senders[mask], n_nodes)
    if visited.sum() >= num_nodes_for_multifocus:
        focus_nodes = np.where(focus_probability > 0)[0]
        if focus_nodes.shape[0] > num_nodes_for_multifocus:
            focus_node_exclude = np.random.choice(focus_nodes, size=focus_nodes.shape[0] - num_nodes_for_multifocus, replace=False)
            focus_nodes = focus_nodes[~np.isin(focus_nodes, focus_node_exclude)]
    else:
        focus_nodes = np.asarray([np.argmax(focus_probability)])

    def choose_target_node(focus_node, key):
        """Picks a random target node for a given focus node."""
        mask_for_focus_node = (senders == focus_node) & mask
        target_edge_ndx = jax.random.choice(key, np.arange(receivers.shape[0]), p=mask_for_focus_node)
        return target_edge_ndx

    focus_mask = np.isin(np.arange(n_nodes), focus_nodes)

    # Pick the target nodes that maximize the number of unique targets.
    best_num_targets = 0
    best_target_ndxs = None
    for _ in range(10):
        rng, key = jax.random.split(rng)
        keys = jax.random.split(key, n_nodes)
        target_ndxs = jax.vmap(choose_target_node)(np.arange(n_nodes), keys)
        num_unique_targets = len(np.unique(target_ndxs[focus_nodes]))
        if num_unique_targets > best_num_targets:
            best_num_targets = num_unique_targets
            best_target_ndxs = target_ndxs

    target_ndxs = best_target_ndxs[focus_nodes]
    target_nodes = receivers[target_ndxs]
    target_mask = np.zeros((num_nodes_for_multifocus, max_targets_per_graph))
    target_species = np.zeros((num_nodes_for_multifocus,))
    target_species[:target_nodes.shape[0]] = graph.nodes.species[target_nodes]

    # Pick neighboring nodes of the same type as the given target node, per focus.
    focus_per_target = senders[target_ndxs]
    target_nodes_all = []
    for i in range(target_ndxs.shape[0]):
        targets = receivers[(senders == focus_per_target[i]) & mask]
        targets_of_same_species = targets[graph.nodes.species[targets] == target_species[i]][:max_targets_per_graph]
        target_mask[i, : len(targets_of_same_species)] = 1
        target_nodes_all.append(np.pad(targets_of_same_species, (0, max_targets_per_graph - len(targets_of_same_species))))
    target_nodes_all = np.asarray(target_nodes_all)

    # if mode == "radius":
    #     target_node_dist = np.linalg.norm(graph.nodes.positions[focus_per_target] - graph.nodes.positions[target_nodes_all], axis=-1)
    #     assert np.all(target_node_dist < max_radius), (
    #         f"Target positions are outside the radial cutoff\nmasked distances: {target_node_dist}"
    #     )

    new_visited = np.concatenate([visited, target_nodes])
    new_visited = np.unique(new_visited)

    sample = _into_fragment(
        graph,
        visited,
        focus_mask,
        target_species_probability,
        target_nodes_all,
        target_mask,
        stop=False,
        max_targets_per_graph=max_targets_per_graph,
        num_nodes_for_multifocus=num_nodes_for_multifocus,
    )

    rng, k = jax.random.split(rng)
    next_node = jax.random.choice(k, target_nodes)
    visited = np.concatenate([visited, [next_node]])
    return rng, visited, sample


def _make_last_fragment(graph, num_species, max_targets_per_graph, num_nodes_for_multifocus):
    n_nodes = len(graph.nodes.positions)
    return _into_fragment(
        graph,
        visited=np.arange(n_nodes),
        focus_mask=np.zeros((n_nodes,), dtype=bool),
        target_species_probability=np.zeros((n_nodes, num_species)),
        target_nodes=np.zeros((num_nodes_for_multifocus, max_targets_per_graph)),
        target_mask=np.zeros((num_nodes_for_multifocus, max_targets_per_graph)),
        stop=True,
        max_targets_per_graph=max_targets_per_graph,
        num_nodes_for_multifocus=num_nodes_for_multifocus,
    )


def _into_fragment(
    graph,
    visited,
    focus_mask,
    target_species_probability,
    target_nodes,
    target_mask,
    stop,
    max_targets_per_graph,
    num_nodes_for_multifocus,
):
    pos = graph.nodes.positions
    species = graph.nodes.species

    target_nodes_reshaped = np.pad(target_nodes, (
        (0, num_nodes_for_multifocus - target_nodes.shape[0]),
        (0, max_targets_per_graph - target_nodes.shape[1])))
    target_nodes_reshaped = target_nodes_reshaped.astype(int)
    target_mask = target_mask.astype(bool)
    target_species = np.zeros((num_nodes_for_multifocus), dtype=int)

    # Check that all target species are the same.
    for i in range(num_nodes_for_multifocus):
        species_i = species[target_nodes_reshaped[i]]
        if target_mask[i].sum() == 0:
            assert len(species_i[target_mask[i]]) == 0
        else:
            assert np.all(species_i[target_mask[i]] == species_i[0])
        target_species[i, ] = species_i[0]

    target_positions = np.zeros((num_nodes_for_multifocus, max_targets_per_graph, 3))
    focus_list = np.arange(len(graph.nodes.positions))[focus_mask]
    for i, (focus, nodes, mask) in enumerate(zip(focus_list, target_nodes, target_mask)):
        nodes = nodes[mask]
        if len(nodes):
            target_positions[i, :nodes.shape[0]] = graph.nodes.positions[nodes] - graph.nodes.positions[focus]

    assert np.all(np.linalg.norm(target_positions, axis=-1) < 5.0), (
        "target positions are outside the radial cutoff\ndistances:",
        np.linalg.norm(target_positions, axis=-1),
    )

    nodes = datatypes.FragmentsNodes(
        positions=pos,
        species=species,
        focus_and_target_species_probs=target_species_probability,
        focus_mask=focus_mask,
    )
    globals = datatypes.FragmentsGlobals(
        stop=np.array([stop], dtype=bool),  # [1]
        target_species=target_species.astype(int),  # [num_nodes_for_multifocus, max_targets_per_graph]
        target_positions=target_positions,  # [num_nodes_for_multifocus, max_targets_per_graph, 3]
        target_positions_mask=target_mask,  # [num_nodes_for_multifocus, max_targets_per_graph]
    )
    globals = jax.tree_map(lambda x: np.expand_dims(x, axis=0), globals)
    graph = graph._replace(nodes=nodes, globals=globals)

    if stop:
        assert len(visited) == len(pos)
        return graph
    else:
        # # put focus node at the beginning
        # visited = _move_first(visited, focus_node)
        visited = np.asarray(visited)

        # return subgraph
        return subgraph(graph, visited)


def _move_first(xs, x):
    return np.roll(xs, -np.where(xs == x)[0][0])


def _normalized_bitcount(xs: np.ndarray, n: int) -> np.ndarray:
    assert xs.ndim == 1
    return np.bincount(xs, minlength=n) / len(xs)


def subgraph(graph: jraph.GraphsTuple, nodes: np.ndarray) -> jraph.GraphsTuple:
    """Extract a subgraph from a graph.

    Args:
        graph: The graph to extract a subgraph from.
        nodes: The indices of the nodes to extract.

    Returns:
        The subgraph.
    """
    assert (
        len(graph.n_edge) == 1 and len(graph.n_node) == 1
    ), "Only single graphs supported."

    # Find all edges that connect to the nodes.
    edges = np.isin(graph.senders, nodes) & np.isin(graph.receivers, nodes)

    new_node_indices = -np.ones(graph.n_node[0], dtype=int)
    new_node_indices[nodes] = np.arange(len(nodes))

    return jraph.GraphsTuple(
        nodes=jax.tree_util.tree_map(lambda x: x[nodes], graph.nodes),
        edges=jax.tree_util.tree_map(lambda x: x[edges], graph.edges),
        globals=graph.globals,
        senders=new_node_indices[graph.senders[edges]],
        receivers=new_node_indices[graph.receivers[edges]],
        n_node=np.array([len(nodes)]),
        n_edge=np.array([np.sum(edges)]),
    )
