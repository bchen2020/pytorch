import subprocess
import torch.fx as fx
import copy
import torch
import math
from typing import Callable, List
from functools import wraps, partial
from dataclasses import dataclass

class ConcreteProp(torch.fx.Interpreter):
    def run_node(self, n):
        result = super().run_node(n)

        found_tensor = False

        def extract_tensor_meta(obj):
            if isinstance(obj, torch.Tensor):
                nonlocal found_tensor
                found_tensor = True
                return obj
            else:
                return obj

        from torch.fx.node import map_aggregate
        concrete_value = map_aggregate(result, extract_tensor_meta)
        if found_tensor:
            n.meta['concrete_value'] = concrete_value
        return result

    def propagate(self, *args):
        return super().run(*args)


def _get_placeholders(graph):
    return list(filter(lambda x: x.op == 'placeholder', graph.nodes))

# inplace modifies node/inps


def _convert_node_to_placeholder(node, inps):
    if node.op == 'output':
        return
    node.op = 'placeholder'
    node.args = ()
    node.target = node.name
    concrete_val = node.meta['concrete_value']
    if isinstance(concrete_val, torch.Tensor):
        inps.append(concrete_val)
    else:
        inps.append(torch.zeros(()))
        for tuple_user in list(node.users):
            _convert_node_to_placeholder(tuple_user, inps)

def generate_repro(fx_g, inps):
        print(f"""
inps = {[(i.shape, i.dtype) for i in inps]}
inps = [torch.zeros(())] + [torch.ones(shape, dtype=dtype, device='cuda') for (shape, dtype) in inps]
{fx_g.code}
f = torch.jit.script(forward)
with torch.jit.fuser("fuser2"):
    for _ in range(5):
        f(*inps)""")

@dataclass
class ReproState:
    graph: fx.Graph
    inps: List[torch.Tensor]

def minifier(fail_f: fx.GraphModule, inps, module_fails, generate_repro: Callable = generate_repro):
    """
    Minimizes a FX graph with given inputs, such that the resulting FX graph still returns True for module_fails.

    Does 2 main strategies:
    1. Truncates suffix: Removes some suffix from the graph and sets a new output.
    2. Delta Debugging: Tries replacing half of the graph with inputs. If fails,
        tries replacing quarter of the graph, etc.

    >>> failing_function = fx.symbolic_trace(f)
    >>> minimize(failing_function, [torch.randn(5)], lambda fx_g, inps: fx_g(*inps))

    note: module_fails returns True if it fails.
    """
    failing_graph = fail_f.graph
    cur_size = len(failing_graph.nodes)

    num_queries = 0
    def graph_fails(graph, inps):
        nonlocal num_queries
        num_queries += 1
        mod = fx.GraphModule(fail_f, graph)
        mod.graph.lint()
        return module_fails(mod, inps)

    ConcreteProp(fail_f).propagate(*inps)
    if not graph_fails(failing_graph, inps):
        raise RuntimeError("Input graph did not fail the tester")
    print(f"Started off with {cur_size} nodes")

    def _register_strategy(strategy: Callable, name: str):
        @wraps(strategy)
        def new_func(old_state: ReproState):
            print(f"Strategy: {name}")
            new_state = strategy(copy.deepcopy(old_state.graph), list(old_state.inps))
            if new_state is not None:
                new_nodes = len(new_state.graph.nodes)
                old_nodes = len(old_state.graph.nodes)
                new_inps = len(new_state.inps)
                old_inps = len(old_state.inps)
                if new_nodes < old_nodes:
                    print(f"SUCCESS: Went from {old_nodes} to {new_nodes} nodes")
                elif new_nodes == old_nodes and new_inps > old_inps:
                    print(f"SUCCESS: Went from {old_inps} to {new_inps} inputs")
                else:
                    raise RuntimeError("Success raised but no progress made?")

                if not graph_fails(new_state.graph, new_state.inps):
                    print("WARNING: Something went wrong, not applying this minification")
                    return None

                return new_state
            else:
                print(f"FAIL: {name}")
            return None

        return new_func

    def register_strategy(name: str):
        return partial(_register_strategy, name=name)

    @register_strategy("Truncate suffix")
    def remove_suffix(cur_graph, cur_inps):
        gap = 2**(math.floor(math.log2(len(cur_graph.nodes))) - 1)
        gap = max(gap, 1)
        tested = set()
        while gap >= 1:
            new_graph = fx.Graph()
            env = {}
            for idx, node in enumerate(cur_graph.nodes):
                new_node = new_graph.node_copy(node, lambda x: env[x])
                if node.op not in ['placeholder', 'output']:
                    if idx % gap == 0 and idx not in tested:
                        output_node = new_graph.output((new_node,))
                        if graph_fails(new_graph, cur_inps) and len(new_graph.nodes) < len(cur_graph.nodes):
                            return ReproState(new_graph, cur_inps)
                        else:
                            tested.add(idx)
                            new_graph.erase_node(output_node)
                env[node] = new_node
            gap //= 2
        print("FAIL: Could not remove suffix")
        return None

    @register_strategy("Remove unused inputs")
    def remove_unused_inputs(cur_graph, cur_inps):
        ph_nodes = _get_placeholders(cur_graph)
        assert len(ph_nodes) == len(cur_inps)

        new_inps = []
        for idx in range(len(ph_nodes)):
            if len(ph_nodes[idx].users) == 0:
                cur_graph.erase_node(ph_nodes[idx])
            else:
                new_inps.append(cur_inps[idx])

        if len(new_inps) < len(cur_inps) and graph_fails(cur_graph, new_inps):
            return ReproState(cur_graph, new_inps)
        else:
            return None

    @register_strategy("Eliminate dead code")
    def eliminate_dead_code(cur_graph, cur_inps):
        if cur_graph.eliminate_dead_code() and graph_fails(cur_graph, cur_inps):
            return ReproState(cur_graph, cur_inps)
        else:
            return None

    def _consolidate_placeholders(cur_graph):
        new_graph = fx.Graph()
        env = {}
        for node in cur_graph.nodes:
            if node.op == 'placeholder':
                new_node = new_graph.node_copy(node, lambda x: env[x])
                env[node] = new_node

        for node in cur_graph.nodes:
            if node.op != 'placeholder':
                new_node = new_graph.node_copy(node, lambda x: env[x])
                env[node] = new_node
        return new_graph

    @register_strategy("Delta Debugging")
    def delta_debugging(cur_graph: fx.Graph, cur_inps):
        num_nodes = len(cur_graph.nodes)
        gap = int(2**math.floor(math.log2(num_nodes)))
        while gap >= 1:
            for start_range in range(0, num_nodes, gap):
                is_removing = False
                new_graph = copy.deepcopy(cur_graph)
                new_inps = cur_inps[:]
                end_range = min(num_nodes, start_range + gap)
                for idx in range(start_range, end_range):
                    new_node = list(new_graph.nodes)[idx]
                    if new_node.op not in ['placeholder', 'output']:
                        is_removing = True
                        _convert_node_to_placeholder(new_node, new_inps)
                if not is_removing:
                    continue
                new_graph = _consolidate_placeholders(new_graph)
                if graph_fails(new_graph, new_inps):
                    return ReproState(new_graph, new_inps)
            gap //= 2

        return None

    failing_state = ReproState(failing_graph, inps)
    while True:
        any_succeeded = False
        strategies = [
            remove_suffix, eliminate_dead_code, remove_unused_inputs,
            delta_debugging, eliminate_dead_code, remove_unused_inputs
        ]
        for strategy in strategies:
            new_state = strategy(failing_state)
            if new_state is not None:
                failing_state = new_state
                any_succeeded = True

        if not any_succeeded:
            break
    print(f"Made {num_queries} queries")
    failing_fx = fx.GraphModule(fail_f, failing_state.graph)
    generate_repro(failing_fx, failing_state.inps)
    return failing_fx, inps
