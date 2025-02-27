# Copyright 2019-2024 Cambridge Quantum Computing
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Methods to allow conversion between Qiskit and pytket circuit classes
"""
from collections import defaultdict
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Union,
    Any,
    Iterable,
    cast,
    Set,
    Tuple,
    TypeVar,
    TYPE_CHECKING,
)
from inspect import signature
from uuid import UUID

import numpy as np

import sympy
import qiskit.circuit.library.standard_gates as qiskit_gates  # type: ignore
from qiskit import (
    ClassicalRegister,
    QuantumCircuit,
    QuantumRegister,
)
from qiskit.circuit import (
    Barrier,
    Instruction,
    InstructionSet,
    Gate,
    ControlledGate,
    Measure,
    Parameter,
    ParameterExpression,
    Reset,
    Clbit,
)
from qiskit.circuit.library import (
    CRYGate,
    RYGate,
    PauliEvolutionGate,
    StatePreparation,
    UnitaryGate,
    Initialize,
)
from pytket.circuit import (
    CircBox,
    Circuit,
    Node,
    Op,
    OpType,
    Unitary1qBox,
    Unitary2qBox,
    Unitary3qBox,
    UnitType,
    CustomGateDef,
    Bit,
    Qubit,
    QControlBox,
    StatePreparationBox,
)
from pytket.unit_id import _TEMP_BIT_NAME
from pytket.pauli import Pauli, QubitPauliString
from pytket.architecture import Architecture, FullyConnected
from pytket.utils import QubitPauliOperator, gen_term_sequence_circuit

from pytket.passes import RebaseCustom

if TYPE_CHECKING:
    from qiskit.providers.backend import BackendV1 as QiskitBackend  # type: ignore
    from qiskit.providers.models.backendproperties import (  # type: ignore
        BackendProperties,
        Nduv,
    )
    from qiskit.circuit.quantumcircuitdata import QuantumCircuitData  # type: ignore
    from pytket.circuit import Op, UnitID

_qiskit_gates_1q = {
    # Exact equivalents (same signature except for factor of pi in each parameter):
    qiskit_gates.HGate: OpType.H,
    qiskit_gates.IGate: OpType.noop,
    qiskit_gates.PhaseGate: OpType.U1,
    qiskit_gates.RGate: OpType.PhasedX,
    qiskit_gates.RXGate: OpType.Rx,
    qiskit_gates.RYGate: OpType.Ry,
    qiskit_gates.RZGate: OpType.Rz,
    qiskit_gates.SdgGate: OpType.Sdg,
    qiskit_gates.SGate: OpType.S,
    qiskit_gates.SXdgGate: OpType.SXdg,
    qiskit_gates.SXGate: OpType.SX,
    qiskit_gates.TdgGate: OpType.Tdg,
    qiskit_gates.TGate: OpType.T,
    qiskit_gates.U1Gate: OpType.U1,
    qiskit_gates.U2Gate: OpType.U2,
    qiskit_gates.U3Gate: OpType.U3,
    qiskit_gates.UGate: OpType.U3,
    qiskit_gates.XGate: OpType.X,
    qiskit_gates.YGate: OpType.Y,
    qiskit_gates.ZGate: OpType.Z,
}

_qiskit_gates_2q = {
    # Exact equivalents (same signature except for factor of pi in each parameter):
    qiskit_gates.CHGate: OpType.CH,
    qiskit_gates.CPhaseGate: OpType.CU1,
    qiskit_gates.CRXGate: OpType.CRx,
    qiskit_gates.CRYGate: OpType.CRy,
    qiskit_gates.CRZGate: OpType.CRz,
    qiskit_gates.CUGate: OpType.CU3,
    qiskit_gates.CU1Gate: OpType.CU1,
    qiskit_gates.CU3Gate: OpType.CU3,
    qiskit_gates.CXGate: OpType.CX,
    qiskit_gates.CSXGate: OpType.CSX,
    qiskit_gates.CYGate: OpType.CY,
    qiskit_gates.CZGate: OpType.CZ,
    qiskit_gates.ECRGate: OpType.ECR,
    qiskit_gates.iSwapGate: OpType.ISWAPMax,
    qiskit_gates.RXXGate: OpType.XXPhase,
    qiskit_gates.RYYGate: OpType.YYPhase,
    qiskit_gates.RZZGate: OpType.ZZPhase,
    qiskit_gates.SwapGate: OpType.SWAP,
}

_qiskit_gates_other = {
    # Exact equivalents (same signature except for factor of pi in each parameter):
    qiskit_gates.C3XGate: OpType.CnX,
    qiskit_gates.C4XGate: OpType.CnX,
    qiskit_gates.CCXGate: OpType.CCX,
    qiskit_gates.CCZGate: OpType.CnZ,
    qiskit_gates.CSwapGate: OpType.CSWAP,
    # Multi-controlled gates (qiskit expects a list of controls followed by the target):
    qiskit_gates.MCXGate: OpType.CnX,
    qiskit_gates.MCXGrayCode: OpType.CnX,
    qiskit_gates.MCXRecursive: OpType.CnX,
    qiskit_gates.MCXVChain: OpType.CnX,
    # Special types:
    Barrier: OpType.Barrier,
    Instruction: OpType.CircBox,
    Gate: OpType.CircBox,
    Measure: OpType.Measure,
    Reset: OpType.Reset,
    Initialize: OpType.StatePreparationBox,
    StatePreparation: OpType.StatePreparationBox,
}

_known_qiskit_gate = {**_qiskit_gates_1q, **_qiskit_gates_2q, **_qiskit_gates_other}

# Some qiskit gates are aliases (e.g. UGate and U3Gate).
# In such cases this reversal will select one or the other.
_known_qiskit_gate_rev = {v: k for k, v in _known_qiskit_gate.items()}

# Ensure U3 maps to UGate. (U3Gate deprecated in Qiskit but equivalent.)
_known_qiskit_gate_rev[OpType.U3] = qiskit_gates.UGate

# There is a bijective mapping, but requires some special parameter conversions
# tk1(a, b, c) = U(b, a-1/2, c+1/2) + phase(-(a+c)/2)
_known_qiskit_gate_rev[OpType.TK1] = qiskit_gates.UGate

# some gates are only equal up to global phase, support their conversion
# from tket -> qiskit
_known_gate_rev_phase = {
    optype: (qgate, 0.0) for optype, qgate in _known_qiskit_gate_rev.items()
}

_known_gate_rev_phase[OpType.V] = (qiskit_gates.SXGate, -0.25)
_known_gate_rev_phase[OpType.Vdg] = (qiskit_gates.SXdgGate, 0.25)

# use minor signature hacks to figure out the string names of qiskit Gate objects
_gate_str_2_optype: Dict[str, OpType] = dict()
for gate, optype in _known_qiskit_gate.items():
    if gate in (
        UnitaryGate,
        Instruction,
        Gate,
        qiskit_gates.MCXGate,  # all of these have special (c*n)x names
        qiskit_gates.MCXGrayCode,
        qiskit_gates.MCXRecursive,
        qiskit_gates.MCXVChain,
    ):
        continue
    sig = signature(gate.__init__)
    # name is only a property of the instance, not the class
    # so initialize with the correct number of dummy variables
    n_params = len([p for p in sig.parameters.values() if p.default is p.empty]) - 1
    name = gate(*([1] * n_params)).name
    _gate_str_2_optype[name] = optype

_gate_str_2_optype_rev = {v: k for k, v in _gate_str_2_optype.items()}
# the aliasing of the name is ok in the reverse map
_gate_str_2_optype_rev[OpType.Unitary1qBox] = "unitary"


def _tk_gate_set(backend: "QiskitBackend") -> Set[OpType]:
    """Set of tket gate types supported by the qiskit backend"""
    config = backend.configuration()
    if config.simulator:
        gate_set = {
            _gate_str_2_optype[gate_str]
            for gate_str in config.basis_gates
            if gate_str in _gate_str_2_optype
        }.union({OpType.Measure, OpType.Reset, OpType.Barrier})
        return gate_set

    else:
        return {
            _gate_str_2_optype[gate_str]
            for gate_str in config.supported_instructions
            if gate_str in _gate_str_2_optype
        }


def _qpo_from_peg(peg: PauliEvolutionGate, qubits: List[Qubit]) -> QubitPauliOperator:
    op = peg.operator
    t = peg.params[0]
    qpodict = {}
    for p, c in zip(op.paulis, op.coeffs):
        if np.iscomplex(c):
            raise ValueError("Coefficient for Pauli {} is non-real.".format(p))
        coeff = param_to_tk(t) * c
        qpslist = []
        pstr = p.to_label()
        for a in pstr:
            if a == "X":
                qpslist.append(Pauli.X)
            elif a == "Y":
                qpslist.append(Pauli.Y)
            elif a == "Z":
                qpslist.append(Pauli.Z)
            else:
                assert a == "I"
                qpslist.append(Pauli.I)
        qpodict[QubitPauliString(qubits, qpslist)] = coeff
    return QubitPauliOperator(qpodict)


def _string_to_circuit(
    circuit_string: str, n_qubits: int, qiskit_instruction: Instruction
) -> Circuit:
    """Helper function to handle strings in QuantumCircuit.initialize
    and QuantumCircuit.prepare_state"""

    circ = Circuit(n_qubits)
    # Check if Instruction is Initialize or Statepreparation
    # If Initialize, add resets
    if isinstance(qiskit_instruction, Initialize):
        for qubit in circ.qubits:
            circ.add_gate(OpType.Reset, [qubit])

    # We iterate through the string in reverse to add the
    # gates in the correct order (endian-ness).
    for count, char in enumerate(reversed(circuit_string)):
        if char == "0":
            pass
        elif char == "1":
            circ.X(count)
        elif char == "+":
            circ.H(count)
        elif char == "-":
            circ.X(count)
            circ.H(count)
        elif char == "r":
            circ.H(count)
            circ.S(count)
        elif char == "l":
            circ.H(count)
            circ.Sdg(count)
        else:
            raise ValueError(
                f"Cannot parse string for character {char}. "
                + "The supported characters are {'0', '1', '+', '-', 'r', 'l'}."
            )

    return circ


class CircuitBuilder:
    def __init__(
        self,
        qregs: List[QuantumRegister],
        cregs: Optional[List[ClassicalRegister]] = None,
        name: Optional[str] = None,
        phase: Optional[sympy.Expr] = None,
    ):
        self.qregs = qregs
        self.cregs = [] if cregs is None else cregs
        self.qbmap = {}
        self.cbmap = {}
        if name is not None:
            self.tkc = Circuit(name=name)
        else:
            self.tkc = Circuit()
        if phase is not None:
            self.tkc.add_phase(phase)
        for reg in qregs:
            self.tkc.add_q_register(reg.name, len(reg))
            for i, qb in enumerate(reg):
                self.qbmap[qb] = Qubit(reg.name, i)
        self.cregmap = {}
        for reg in self.cregs:
            tk_reg = self.tkc.add_c_register(reg.name, len(reg))
            self.cregmap.update({reg: tk_reg})
            for i, cb in enumerate(reg):
                self.cbmap[cb] = Bit(reg.name, i)

    def circuit(self) -> Circuit:
        return self.tkc

    def add_xs(
        self,
        num_ctrl_qubits: Optional[int],
        ctrl_state: Optional[Union[str, int]],
        qargs: List["Qubit"],
    ) -> None:
        if ctrl_state is not None:
            assert isinstance(num_ctrl_qubits, int)
            assert num_ctrl_qubits >= 0
            c = int(ctrl_state, 2) if isinstance(ctrl_state, str) else int(ctrl_state)
            assert c >= 0 and (c >> num_ctrl_qubits) == 0
            for i in range(num_ctrl_qubits):
                if ((c >> i) & 1) == 0:
                    self.tkc.X(self.qbmap[qargs[i]])

    def add_qiskit_data(self, data: "QuantumCircuitData") -> None:
        for instr, qargs, cargs in data:
            condition_kwargs = {}
            if instr.condition is not None:
                if type(instr.condition[0]) == ClassicalRegister:
                    cond_reg = self.cregmap[instr.condition[0]]
                    condition_kwargs = {
                        "condition_bits": [cond_reg[k] for k in range(len(cond_reg))],
                        "condition_value": instr.condition[1],
                    }
                elif type(instr.condition[0]) == Clbit:
                    cond_reg = self.cregmap[instr.condition[0].register]
                    condition_kwargs = {
                        "condition_bits": [cond_reg[instr.condition[0].index]],
                        "condition_value": instr.condition[1],
                    }
                else:
                    raise NotImplementedError(
                        "condition must contain classical bit or register"
                    )

            # Controlled operations may be controlled on values other than all-1. Handle
            # this by prepending and appending X gates on the control qubits.
            ctrl_state, num_ctrl_qubits = None, None
            try:
                ctrl_state = instr.ctrl_state
                num_ctrl_qubits = instr.num_ctrl_qubits
            except AttributeError:
                pass
            self.add_xs(num_ctrl_qubits, ctrl_state, qargs)
            optype = None
            if isinstance(instr, ControlledGate):
                if instr.base_class in _known_qiskit_gate:
                    # First we check if the gate is in _known_qiskit_gate
                    # this avoids CZ being converted to CnZ
                    optype = _known_qiskit_gate[instr.base_class]
                elif instr.base_gate.base_class is qiskit_gates.RYGate:
                    optype = OpType.CnRy
                elif instr.base_gate.base_class is qiskit_gates.YGate:
                    optype = OpType.CnY
                elif instr.base_gate.base_class is qiskit_gates.ZGate:
                    optype = OpType.CnZ
                else:
                    if instr.base_gate.base_class in _known_qiskit_gate:
                        optype = OpType.QControlBox  # QControlBox case handled below
                    else:
                        raise NotImplementedError(
                            f"qiskit ControlledGate with base gate {instr.base_gate}"
                            + "not implemented"
                        )
            elif type(instr) in [PauliEvolutionGate, UnitaryGate]:
                pass  # Special handling below
            else:
                try:
                    optype = _known_qiskit_gate[instr.base_class]
                except KeyError:
                    raise NotImplementedError(
                        f"Conversion of qiskit's {instr.name} instruction is "
                        + "currently unsupported by qiskit_to_tk. Consider "
                        + "using QuantumCircuit.decompose() before attempting "
                        + "conversion."
                    )
            qubits = [self.qbmap[qbit] for qbit in qargs]
            bits = [self.cbmap[bit] for bit in cargs]

            if optype == OpType.QControlBox:
                base_tket_gate = _known_qiskit_gate[instr.base_gate.base_class]
                params = [param_to_tk(p) for p in instr.base_gate.params]
                n_base_qubits = instr.base_gate.num_qubits
                sub_circ = Circuit(n_base_qubits)
                # use base gate name for the CircBox (shows in renderer)
                sub_circ.name = instr.base_gate.name.capitalize()
                sub_circ.add_gate(base_tket_gate, params, list(range(n_base_qubits)))
                c_box = CircBox(sub_circ)
                q_ctrl_box = QControlBox(c_box, instr.num_ctrl_qubits)
                self.tkc.add_qcontrolbox(q_ctrl_box, qubits)

            elif isinstance(instr, (Initialize, StatePreparation)):
                # Check how Initialize or StatePrep is constructed
                if isinstance(instr.params[0], str):
                    # Parse string to get the right single qubit gates
                    circuit_string = "".join(instr.params)
                    circuit = _string_to_circuit(
                        circuit_string, instr.num_qubits, qiskit_instruction=instr
                    )
                    self.tkc.add_circuit(circuit, qubits)

                elif isinstance(instr.params, list) and len(instr.params) != 1:
                    amplitude_list = instr.params
                    if isinstance(instr, Initialize):
                        pytket_state_prep_box = StatePreparationBox(
                            amplitude_list, with_initial_reset=True  # type: ignore
                        )
                    else:
                        pytket_state_prep_box = StatePreparationBox(
                            amplitude_list, with_initial_reset=False  # type: ignore
                        )
                    # Need to reverse qubits here (endian-ness)
                    reversed_qubits = list(reversed(qubits))
                    self.tkc.add_gate(pytket_state_prep_box, reversed_qubits)

                elif isinstance(instr.params[0], complex) and len(instr.params) == 1:
                    # convert int to a binary string and apply X for |1>
                    integer_parameter = int(instr.params[0].real)
                    bit_string = bin(integer_parameter)[2:]
                    circuit = _string_to_circuit(
                        bit_string, instr.num_qubits, qiskit_instruction=instr
                    )
                    self.tkc.add_circuit(circuit, qubits)

            elif type(instr) == PauliEvolutionGate:
                qpo = _qpo_from_peg(instr, qubits)
                empty_circ = Circuit(len(qargs))
                circ = gen_term_sequence_circuit(qpo, empty_circ)
                ccbox = CircBox(circ)
                self.tkc.add_circbox(ccbox, qubits)
            elif type(instr) == UnitaryGate:
                # Note reversal of qubits, to account for endianness (pytket unitaries
                # are ILO-BE == DLO-LE; qiskit unitaries are ILO-LE == DLO-BE).
                params = instr.params
                assert len(params) == 1
                u = cast(np.ndarray, params[0])
                assert len(cargs) == 0
                n = len(qubits)
                if n == 0:
                    assert u.shape == (1, 1)
                    self.tkc.add_phase(np.angle(u[0][0]) / np.pi)
                elif n == 1:
                    assert u.shape == (2, 2)
                    u1box = Unitary1qBox(u)
                    self.tkc.add_unitary1qbox(u1box, qubits[0], **condition_kwargs)
                elif n == 2:
                    assert u.shape == (4, 4)
                    u2box = Unitary2qBox(u)
                    self.tkc.add_unitary2qbox(
                        u2box, qubits[1], qubits[0], **condition_kwargs
                    )
                elif n == 3:
                    assert u.shape == (8, 8)
                    u3box = Unitary3qBox(u)
                    self.tkc.add_unitary3qbox(
                        u3box, qubits[2], qubits[1], qubits[0], **condition_kwargs
                    )
                else:
                    raise NotImplementedError(
                        f"Conversion of {n}-qubit unitary gates not supported"
                    )

            elif optype == OpType.Barrier:
                self.tkc.add_barrier(qubits)
            elif optype == OpType.CircBox:
                qregs = (
                    [QuantumRegister(instr.num_qubits, "q")]
                    if instr.num_qubits > 0
                    else []
                )
                cregs = (
                    [ClassicalRegister(instr.num_clbits, "c")]
                    if instr.num_clbits > 0
                    else []
                )
                builder = CircuitBuilder(qregs, cregs)
                builder.add_qiskit_data(instr.definition)
                subc = builder.circuit()
                subc.name = instr.name
                self.tkc.add_circbox(CircBox(subc), qubits + bits, **condition_kwargs)  # type: ignore

            elif optype == OpType.CU3 and type(instr) == qiskit_gates.CUGate:
                if instr.params[-1] == 0:
                    self.tkc.add_gate(
                        optype,
                        [param_to_tk(p) for p in instr.params[:-1]],
                        qubits,
                        **condition_kwargs,
                    )
                else:
                    raise NotImplementedError("CUGate with nonzero phase")
            else:
                params = [param_to_tk(p) for p in instr.params]
                self.tkc.add_gate(optype, params, qubits + bits, **condition_kwargs)  # type: ignore

            self.add_xs(num_ctrl_qubits, ctrl_state, qargs)


def qiskit_to_tk(qcirc: QuantumCircuit, preserve_param_uuid: bool = False) -> Circuit:
    """
    Converts a qiskit :py:class:`qiskit.QuantumCircuit` to a pytket :py:class:`Circuit`.

    :param qcirc: A circuit to be converted
    :type qcirc: QuantumCircuit
    :param preserve_param_uuid: Whether to preserve symbolic Parameter uuids
        by appending them to the tket Circuit symbol names as "_UUID:<uuid>".
        This can be useful if you want to reassign Parameters after conversion
        to tket and back, as it is necessary for Parameter object equality
        to be preserved.
    :type preserve_param_uuid: bool
    :return: The converted circuit
    :rtype: Circuit
    """
    circ_name = qcirc.name
    # Parameter uses a hidden _uuid for equality check
    # we optionally preserve this in parameter name for later use
    if preserve_param_uuid:
        updates = {p: Parameter(f"{p.name}_UUID:{p._uuid}") for p in qcirc.parameters}
        qcirc = cast(QuantumCircuit, qcirc.assign_parameters(updates))

    builder = CircuitBuilder(
        qregs=qcirc.qregs,
        cregs=qcirc.cregs,
        name=circ_name,
        phase=param_to_tk(qcirc.global_phase),
    )
    builder.add_qiskit_data(qcirc.data)
    return builder.circuit()


def param_to_tk(p: Union[float, ParameterExpression]) -> sympy.Expr:
    if isinstance(p, ParameterExpression):
        symexpr = p._symbol_expr
        try:
            return symexpr._sympy_() / sympy.pi  # type: ignore
        except AttributeError:
            return symexpr / sympy.pi  # type: ignore
    else:
        return p / sympy.pi  # type: ignore


def param_to_qiskit(
    p: sympy.Expr, symb_map: Dict[Parameter, sympy.Symbol]
) -> Union[float, ParameterExpression]:
    ppi = p * sympy.pi
    if len(ppi.free_symbols) == 0:
        return float(ppi.evalf())
    else:
        return ParameterExpression(symb_map, ppi)


def _get_params(
    op: Op, symb_map: Dict[Parameter, sympy.Symbol]
) -> List[Union[float, ParameterExpression]]:
    return [param_to_qiskit(p, symb_map) for p in op.params]  # type: ignore


def append_tk_command_to_qiskit(
    op: "Op",
    args: List["UnitID"],
    qcirc: QuantumCircuit,
    qregmap: Dict[str, QuantumRegister],
    cregmap: Dict[str, ClassicalRegister],
    symb_map: Dict[Parameter, sympy.Symbol],
    range_preds: Dict[Bit, Tuple[List["UnitID"], int]],
) -> InstructionSet:
    optype = op.type
    if optype == OpType.Measure:
        qubit = args[0]
        bit = args[1]
        qb = qregmap[qubit.reg_name][qubit.index[0]]
        b = cregmap[bit.reg_name][bit.index[0]]
        return qcirc.measure(qb, b)

    if optype == OpType.Reset:
        qb = qregmap[args[0].reg_name][args[0].index[0]]
        return qcirc.reset(qb)

    if optype in [OpType.CircBox, OpType.ExpBox, OpType.PauliExpBox, OpType.CustomGate]:
        subcircuit = op.get_circuit()  # type: ignore
        subqc = tk_to_qiskit(subcircuit)
        qargs = []
        cargs = []
        for a in args:
            if a.type == UnitType.qubit:
                qargs.append(qregmap[a.reg_name][a.index[0]])
            else:
                cargs.append(cregmap[a.reg_name][a.index[0]])
        if optype == OpType.CustomGate:
            instruc = subqc.to_gate()
            instruc.name = op.get_name()
        else:
            instruc = subqc.to_instruction()
        return qcirc.append(instruc, qargs, cargs)
    if optype in [OpType.Unitary1qBox, OpType.Unitary2qBox, OpType.Unitary3qBox]:
        qargs = [qregmap[q.reg_name][q.index[0]] for q in args]
        u = op.get_matrix()  # type: ignore
        g = UnitaryGate(u, label="unitary")
        # Note reversal of qubits, to account for endianness (pytket unitaries are
        # ILO-BE == DLO-LE; qiskit unitaries are ILO-LE == DLO-BE).
        return qcirc.append(g, qargs=list(reversed(qargs)))
    if optype == OpType.StatePreparationBox:
        qargs = [qregmap[q.reg_name][q.index[0]] for q in args]
        statevector_array = op.get_statevector()  # type: ignore
        # check if the StatePreparationBox contains resets
        if op.with_initial_reset():  # type: ignore
            initializer = Initialize(statevector_array)
            return qcirc.append(initializer, qargs=list(reversed(qargs)))
        else:
            qiskit_state_prep_box = StatePreparation(statevector_array)
            return qcirc.append(qiskit_state_prep_box, qargs=list(reversed(qargs)))

    if optype == OpType.Barrier:
        if any(q.type == UnitType.bit for q in args):
            raise NotImplementedError(
                "Qiskit Barriers are not defined for classical bits."
            )
        qargs = [qregmap[q.reg_name][q.index[0]] for q in args]
        g = Barrier(len(args))
        return qcirc.append(g, qargs=qargs)
    if optype == OpType.RangePredicate:
        if op.lower != op.upper:  # type: ignore
            raise NotImplementedError
        range_preds[args[-1]] = (args[:-1], op.lower)  # type: ignore
        # attach predicate to bit,
        # subsequent conditional will handle it
        return Instruction("", 0, 0, [])
    if optype == OpType.Conditional:
        if op.op.type == OpType.Phase:  # type: ignore
            # conditional phase not supported
            return InstructionSet()
        if args[0] in range_preds:
            assert op.value == 1  # type: ignore
            condition_bits, value = range_preds[args[0]]  # type: ignore
            del range_preds[args[0]]  # type: ignore
            args = condition_bits + args[1:]
            width = len(condition_bits)
        else:
            width = op.width  # type: ignore
            value = op.value  # type: ignore
        regname = args[0].reg_name
        for i, a in enumerate(args[:width]):
            if a.reg_name != regname:
                raise NotImplementedError("Conditions can only use a single register")
        instruction = append_tk_command_to_qiskit(
            op.op, args[width:], qcirc, qregmap, cregmap, symb_map, range_preds  # type: ignore
        )
        if len(cregmap[regname]) == width:
            for i, a in enumerate(args[:width]):
                if a.index != [i]:
                    raise NotImplementedError(
                        """Conditions must be an entire register in\
 order or only one bit of one register"""
                    )

            instruction.c_if(cregmap[regname], value)
        elif width == 1:
            instruction.c_if(cregmap[regname][args[0].index[0]], value)
        else:
            raise NotImplementedError(
                """Conditions must be an entire register in\
order or only one bit of one register"""
            )

        return instruction
    # normal gates
    qargs = [qregmap[q.reg_name][q.index[0]] for q in args]
    if optype == OpType.CnX:
        return qcirc.mcx(qargs[:-1], qargs[-1])
    if optype == OpType.CnY:
        return qcirc.append(qiskit_gates.YGate().control(len(qargs) - 1), qargs)
    if optype == OpType.CnZ:
        return qcirc.append(qiskit_gates.ZGate().control(len(qargs) - 1), qargs)
    if optype == OpType.CnRy:
        # might as well do a bit more checking
        assert len(op.params) == 1
        alpha = param_to_qiskit(op.params[0], symb_map)  # type: ignore
        assert len(qargs) >= 2
        if len(qargs) == 2:
            # presumably more efficient; single control only
            new_gate = CRYGate(alpha)
        else:
            new_gate = RYGate(alpha).control(len(qargs) - 1)
        qcirc.append(new_gate, qargs)
        return qcirc

    if optype == OpType.CU3:
        params = _get_params(op, symb_map) + [0]
        return qcirc.append(qiskit_gates.CUGate(*params), qargs=qargs)

    if optype == OpType.TK1:
        params = _get_params(op, symb_map)
        half = ParameterExpression(symb_map, sympy.pi / 2)
        qcirc.global_phase += -params[0] / 2 - params[2] / 2
        return qcirc.append(
            qiskit_gates.UGate(params[1], params[0] - half, params[2] + half),
            qargs=qargs,
        )

    if optype == OpType.Phase:
        params = _get_params(op, symb_map)
        assert len(params) == 1
        qcirc.global_phase += params[0]
        return InstructionSet()

    # others are direct translations
    try:
        gatetype, phase = _known_gate_rev_phase[optype]
    except KeyError as error:
        raise NotImplementedError(
            "Cannot convert tket Op to Qiskit gate: " + op.get_name()
        ) from error
    params = _get_params(op, symb_map)
    g = gatetype(*params)
    if type(phase) == float:
        qcirc.global_phase += phase * np.pi
    else:
        qcirc.global_phase += phase * sympy.pi
    return qcirc.append(g, qargs=qargs)


# Define varibles for RebaseCustom
_cx_replacement = Circuit(2).CX(0, 1)

# The set of tket gates that can be converted directly to qiskit gates
_supported_tket_gates = set(_known_gate_rev_phase.keys())

_additional_multi_controlled_gates = {OpType.CnY, OpType.CnZ, OpType.CnRy}

# tket gates which are protected from being decomposed in the rebase
_protected_tket_gates = (
    _supported_tket_gates
    | _additional_multi_controlled_gates
    | {OpType.Unitary1qBox, OpType.Unitary2qBox, OpType.Unitary3qBox}
    | {OpType.CustomGate}
)


Param = Union[float, "sympy.Expr"]  # Type for TK1 and U3 parameters


# Use the U3 gate for tk1_replacement as this is a member of _supported_tket_gates
def _tk1_to_u3(a: Param, b: Param, c: Param) -> Circuit:
    tk1_circ = Circuit(1)
    tk1_circ.add_gate(OpType.U3, [b, a - 1 / 2, c + 1 / 2], [0]).add_phase(-(a + c) / 2)
    return tk1_circ


# This is a rebase to the set of tket gates which have an exact substitution in qiskit
supported_gate_rebase = RebaseCustom(_protected_tket_gates, _cx_replacement, _tk1_to_u3)


def tk_to_qiskit(
    tkcirc: Circuit, replace_implicit_swaps: bool = False
) -> QuantumCircuit:
    """
    Converts a pytket :py:class:`Circuit` to a qiskit :py:class:`qiskit.QuantumCircuit`.

    In many cases there will be a qiskit gate to exactly replace each tket gate.
    If no exact replacement can be found for a part of the circuit then an equivalent
    circuit will be returned using the tket gates which are supported in qiskit.

    :param tkcirc: A :py:class:`Circuit` to be converted
    :type tkcirc: Circuit
    :param replace_implicit_swaps: Implement implicit permutation by adding SWAPs
        to the end of the circuit.
    :type replace_implicit_swaps: bool
    :return: The converted circuit
    :rtype: QuantumCircuit
    """
    tkc = tkcirc.copy()  # Make a local copy of tkcirc
    if replace_implicit_swaps:
        tkc.replace_implicit_wire_swaps()
    qcirc = QuantumCircuit(name=tkc.name)
    qreg_sizes: Dict[str, int] = {}
    for qb in tkc.qubits:
        if len(qb.index) != 1:
            raise NotImplementedError("Qiskit registers must use a single index")
        if (qb.reg_name not in qreg_sizes) or (qb.index[0] >= qreg_sizes[qb.reg_name]):
            qreg_sizes.update({qb.reg_name: qb.index[0] + 1})
    creg_sizes: Dict[str, int] = {}
    for b in tkc.bits:
        if len(b.index) != 1:
            raise NotImplementedError("Qiskit registers must use a single index")
        # names with underscore not supported, and _TEMP_BIT_NAME should not be needed
        # for qiskit compatible classical control circuits
        if b.reg_name != _TEMP_BIT_NAME and (
            (b.reg_name not in creg_sizes) or (b.index[0] >= creg_sizes[b.reg_name])
        ):
            creg_sizes.update({b.reg_name: b.index[0] + 1})
    qregmap = {}
    for reg_name, size in qreg_sizes.items():
        qis_reg = QuantumRegister(size, reg_name)
        qregmap.update({reg_name: qis_reg})
        qcirc.add_register(qis_reg)
    cregmap = {}
    for reg_name, size in creg_sizes.items():
        qis_reg = ClassicalRegister(size, reg_name)
        cregmap.update({reg_name: qis_reg})
        qcirc.add_register(qis_reg)
    symb_map = {Parameter(str(s)): s for s in tkc.free_symbols()}
    range_preds: Dict[Bit, Tuple[List["UnitID"], int]] = dict()

    # Apply a rebase to the set of pytket gates which have replacements in qiskit
    supported_gate_rebase.apply(tkc)

    for command in tkc:
        append_tk_command_to_qiskit(
            command.op, command.args, qcirc, qregmap, cregmap, symb_map, range_preds
        )
    qcirc.global_phase += param_to_qiskit(tkc.phase, symb_map)  # type: ignore

    # if UUID stored in name, set parameter uuids accordingly (see qiskit_to_tk)
    updates = dict()
    for p in qcirc.parameters:
        name_spl = p.name.split("_UUID:", 2)
        if len(name_spl) == 2:
            p_name, uuid_str = name_spl
            uuid = UUID(uuid_str)
            # See Parameter.__init__() in qiskit/circuit/parameter.py.
            new_p = Parameter(p_name)
            new_p._uuid = uuid
            new_p._parameter_keys = frozenset(((p_name, uuid),))
            new_p._hash = hash((new_p._parameter_keys, new_p._symbol_expr))
            updates[p] = new_p
    qcirc.assign_parameters(updates, inplace=True)

    return qcirc


def process_characterisation(backend: "QiskitBackend") -> Dict[str, Any]:
    """Convert a :py:class:`qiskit.providers.backend.Backendv1` to a dictionary
     containing device Characteristics

    :param backend: A backend to be converted
    :type backend: Backendv1
    :return: A dictionary containing device characteristics
    :rtype: dict
    """

    # TODO explicitly check for and separate 1 and 2 qubit gates
    properties = cast("BackendProperties", backend.properties())

    def return_value_if_found(iterator: Iterable["Nduv"], name: str) -> Optional[Any]:
        try:
            first_found = next(filter(lambda item: item.name == name, iterator))
        except StopIteration:
            return None
        if hasattr(first_found, "value"):
            return first_found.value
        return None

    config = backend.configuration()
    coupling_map = config.coupling_map
    n_qubits = config.n_qubits
    if coupling_map is None:
        # Assume full connectivity
        arc: Union[FullyConnected, Architecture] = FullyConnected(n_qubits)
    else:
        arc = Architecture(coupling_map)

    link_errors: dict = defaultdict(dict)
    node_errors: dict = defaultdict(dict)
    readout_errors: dict = {}

    t1_times = []
    t2_times = []
    frequencies = []
    gate_times = []

    if properties is not None:
        for index, qubit_info in enumerate(properties.qubits):
            t1_times.append([index, return_value_if_found(qubit_info, "T1")])
            t2_times.append([index, return_value_if_found(qubit_info, "T2")])
            frequencies.append([index, return_value_if_found(qubit_info, "frequency")])
            # readout error as a symmetric 2x2 matrix
            offdiag = return_value_if_found(qubit_info, "readout_error")
            if offdiag:
                diag = 1.0 - offdiag
                readout_errors[index] = [[diag, offdiag], [offdiag, diag]]
            else:
                readout_errors[index] = None

        for gate in properties.gates:
            name = gate.gate
            if name in _gate_str_2_optype:
                optype = _gate_str_2_optype[name]
                qubits = gate.qubits
                gate_error = return_value_if_found(gate.parameters, "gate_error")
                gate_error = gate_error if gate_error else 0.0
                gate_length = return_value_if_found(gate.parameters, "gate_length")
                gate_length = gate_length if gate_length else 0.0
                gate_times.append([name, qubits, gate_length])
                # add gate fidelities to their relevant lists
                if len(qubits) == 1:
                    node_errors[qubits[0]].update({optype: gate_error})
                elif len(qubits) == 2:
                    link_errors[tuple(qubits)].update({optype: gate_error})
                    opposite_link = tuple(qubits[::-1])
                    if opposite_link not in coupling_map:
                        # to simulate a worse reverse direction square the fidelity
                        link_errors[opposite_link].update({optype: 2 * gate_error})

    # map type (k1 -> k2) -> v[k1] -> v[k2]
    K1 = TypeVar("K1")
    K2 = TypeVar("K2")
    V = TypeVar("V")
    convert_keys_t = Callable[[Callable[[K1], K2], Dict[K1, V]], Dict[K2, V]]
    # convert qubits to architecture Nodes
    convert_keys: convert_keys_t = lambda f, d: {f(k): v for k, v in d.items()}
    node_errors = convert_keys(lambda q: Node(q), node_errors)
    link_errors = convert_keys(lambda p: (Node(p[0]), Node(p[1])), link_errors)
    readout_errors = convert_keys(lambda q: Node(q), readout_errors)

    characterisation: Dict[str, Any] = dict()
    characterisation["NodeErrors"] = node_errors
    characterisation["EdgeErrors"] = link_errors
    characterisation["ReadoutErrors"] = readout_errors
    characterisation["Architecture"] = arc
    characterisation["t1times"] = t1_times
    characterisation["t2times"] = t2_times
    characterisation["Frequencies"] = frequencies
    characterisation["GateTimes"] = gate_times

    return characterisation


def get_avg_characterisation(
    characterisation: Dict[str, Any]
) -> Dict[str, Dict[Node, float]]:
    """
    Convert gate-specific characterisation into readout, one- and two-qubit errors

    Used to convert a typical output from `process_characterisation` into an input
    noise characterisation for NoiseAwarePlacement
    """

    K = TypeVar("K")
    V1 = TypeVar("V1")
    V2 = TypeVar("V2")
    map_values_t = Callable[[Callable[[V1], V2], Dict[K, V1]], Dict[K, V2]]
    map_values: map_values_t = lambda f, d: {k: f(v) for k, v in d.items()}

    node_errors = cast(Dict[Node, Dict[OpType, float]], characterisation["NodeErrors"])
    link_errors = cast(
        Dict[Tuple[Node, Node], Dict[OpType, float]], characterisation["EdgeErrors"]
    )
    readout_errors = cast(
        Dict[Node, List[List[float]]], characterisation["ReadoutErrors"]
    )

    avg: Callable[[Dict[Any, float]], float] = lambda xs: sum(xs.values()) / len(xs)
    avg_mat: Callable[[List[List[float]]], float] = (
        lambda xs: (xs[0][1] + xs[1][0]) / 2.0
    )
    avg_readout_errors = map_values(avg_mat, readout_errors)
    avg_node_errors = map_values(avg, node_errors)
    avg_link_errors = map_values(avg, link_errors)

    return {
        "node_errors": avg_node_errors,
        "edge_errors": avg_link_errors,
        "readout_errors": avg_readout_errors,
    }
