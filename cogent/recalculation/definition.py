#!/usr/bin/env python
"""A recalculation engine, something like a spreadsheet.

Goals:
 - Allow construction of a calculation in a flexible and declarative way.
 - Enable caching at any step in the calculation where it makes sense.

Terms:
 - Definition - defines one cachable step in a complex calculation.
 - ParameterController - Sets parameter scope rules on a DAG of Definitions.
 - Calculator - An instance of an internally caching function.
 - Category - An arbitrary label.
 - Dimension - A named set of categories.
 - Scope - A subset of the categories from each dimension.
 - Setting - A variable (Var) or constant (ConstVal).
 - Assignments - A mapping from Scopes to Settings.
 - Cell - Evaluates one Scope of one Definition.
 - OptPar - A cell with indegree 0.

Structure:
 - A Calculator holds a list of Cells: OptPars and EvaluatedCells.
 - EvaluatedCells take their arguments from other Cells.
 - Each type of cell (motifs, Qs, Psubs) made by a different CalculationDefn.
 - No two cells from the same CalculationDefn have the same inputs, so nothing
   is calculated twice.

Interface:
  1) Define a function for each step in the calculation.
  2) Instantiate a DAG of ParamDefns and CalcDefns, each CalcDefn like
     CalcDefn(f)(*args) where 'f' is one of your functions and the '*args'
     are Defns that correspond to the arguments of 'f'.
  3) With your final CalcDefn called say 'top', PC = ParameterController(top)
     to get a ParameterController.
  4) PC.assignAll(param, value=value, **scope) to define the parameter
     scopes.  'value' can be a constant float or an instance of Var.
  5) calculator = PC.makeCalculator() to get a live Calculator.
  6) calculator.optimise() etc.

Caching:
  In addition to the caching provided by the update strategy (not recalculating
  anything that hasn't changed), the calculator keeps a 1-deep cache of the
  previous value for each cell so that it has a 1-deep undo capability.  This
  is ideal for the behaviour of a one-change-at-a-time simanneal optimiser,
  which backtracks when a new value isn't accepted, ie it tries sequences like:
    [0,0,0] [0,0,3] [0,8,0] [7,0,0] [0,0,4] [0,6,0] ...
  when it isn't making progress, and
    [0,0,0] [0,0,3] [0,8,3] [7,8,3] [7,8,9] ...
  when it's having a lucky streak.
  
  Each cell knows when it's out of date, but doesn't know why (ie: what input
  changed) which limits the undo strategy to all-or-nothing.  An optimiser that
  tried values
    [0,0,0] [0,3,8] [0,3,0] ...
  (ie: the third step is a recombination of the previous two) would not get
  any help from caching.  This does keep things simple and fast though.

Recycling:
  If defn.recycling is True then defn.calc() will be passed the previous
  result as its first argument so it can be reused.  This is to avoid
  having to reallocate memory for say a large numpy array just at the
  very moment that an old one of the same shape is being disposed of.
  To prevent recycling from invalidating the caching system 3 values are
  stored for each cell - current, previous and spare.  The spare value is
  the one to be used next for recycling.
"""
from __future__ import division
import numpy
import logging

# In this module we bring together scopes, settings and calculations.
# Most of the classes are 'Defns' with their superclasses in scope.py.
# These supply a makeEvaluator() method which instantiates 'Cell'
# classes from calculation.py

from calculation import EvaluatedCell, OptPar, LogOptPar, ConstCell, \
        Calculator

from scope import Evaluator, _NonLeafDefn, _LeafDefn, _Defn, \
        _ParameterController, SelectFromDimension

from setting import Var, ConstVal, Whole, Part

from cogent.util.dict_array import DictArrayTemplate
from cogent.maths.stats.distribution import chdtri
from cogent.util import parallel

LOG = logging.getLogger('cogent')

__author__ = "Peter Maxwell"
__copyright__ = "Copyright 2007-2008, The Cogent Project"
__credits__ = ["Peter Maxwell", "Gavin Huttley"]
__license__ = "GPL"
__version__ = "1.3.0.dev"
__maintainer__ = "Peter Maxwell"
__email__ = "pm67nz@gmail.com"
__status__ = "Production"

def theOneItemIn(items):
    assert len(items) == 1, items
    return items[0]


class ParameterController(_ParameterController):
    
    def assignAll(self, par_name, scope_spec=None, value=None,
            lower=None, upper=None, const=None, independent=None):
        settings = []
        PC = self.defn_for[par_name]
        
        if const is None:
            const = PC.const_by_default
        
        for scope in PC.interpretScopes(
                independent=independent, **(scope_spec or {})):
            if value is None:
                values = PC.getAllDefaultValues(scope)
                values.sort()
                s_value = values[len(values)//2]
                if values != [s_value] * len(values):
                    LOG.warning("Used '%s' median of %s" % (par_name, s_value))
            else:
                s_value = value
            if const:
                setting = ConstVal(s_value)
            else:
                (s_lower, s_upper) = PC.getCurrentBounds(scope)
                if lower is not None: s_lower = lower
                if upper is not None: s_upper = upper
                setting = Var((s_lower, s_value, s_upper))
            settings.append((scope, setting))
        PC.assign(settings)
        self.update([PC])
    
    def assignTotal(self, par_name, scope_spec, total=None):
        PC = self.defn_for[par_name]
        for scope in PC.interpretScopes(independent=False, **scope_spec):
            if total is None:
                total = sum(PC.getAllDefaultValues(scope))
            # should check scope is 1D
            N = len(scope)
            whole = Whole(N, PartitionDefn,
                    bounds = (0.0, total/N, total),
                    default = [1.0*total/N] * N,
                    name = PC.name + '_partition')
            PC.assign([(set([s]), whole.getPart()) for s in scope])
        self.update([PC])
    
    def makeCalculator(self, force_parallel=None, **kw):
        # self.makeParallelCalculator() actually makes calculators, this just
        # wraps it to find the best degree of parallelisation for this
        # particular calculation on the current hardware/MPI system.
        comm = parallel.getCommunicator()
        if force_parallel:
            return self._makeParallelCalculator(split=comm.size, **kw)
        best_lf = self._makeParallelCalculator(split=1, **kw)
        if (comm.size == 1 or
                'parallel_context' not in self.defn_for or
                len(best_lf.getValueArray()) == 0 or
                force_parallel is not None):
            return best_lf
        best_speed = baseline_speed = best_lf.measureEvalsPerSecond()
        parallelisation_achieved = 1
        desc = "no"
        for split in range(2, comm.size+1):
            if comm.size % split:
                continue
            lf = self._makeParallelCalculator(split=split, **kw)
            speed = lf.measureEvalsPerSecond()
            LOG.info("%s-way parallelisation gave %s speedup" % (
                    split, speed / baseline_speed))
            if speed > best_speed:
                best_lf = lf
                best_speed = speed
                parallelisation_achieved = split
                desc = "%s-way" % parallelisation_achieved
            else:
                break
        if parallelisation_achieved < comm.size:
            # assuming no other part of the LF can use them:
            if not parallel.inefficiency_forgiven:
                LOG.warning("Using %s parallelism even though %s cpus are "\
                    "available, MPI overhead greater than gain, CPUs are "\
                    "being wasted" % (desc, comm.size))
        return best_lf
    
    def _makeParallelCalculator(self, split=None, **kw):
        comm = parallel.getCommunicator()
        kw['overall_parallel_context'] = comm
        if split is None:
            split = 1
        (parallel_context, parallel_subcontext) = \
                parallel.getSplitCommunicators(split)
        if 'parallel_context' in self.defn_for:
            self.assignAll(
                'parallel_context', value=parallel_context, const=True)
        if parallel_subcontext.size < comm.size:
            kw['remaining_parallel_context'] = parallel_subcontext
        return self._makeCalculator(**kw)
    
    def _makeCalculator(self, calculatorClass=None, variable=None, **kw):
        if calculatorClass is None:
            calculatorClass = Calculator
        evs = self._makeEvaluators(variable=variable)
        return calculatorClass(evs, **kw)
    
    def _makeEvaluators(self, variable=None):
        evs = []
        input_soup = {}
        for pd in self.defns:
            pd.update()
            pd_evs = pd.makeEvaluators(input_soup, variable)
            evs.extend(pd_evs)
            input_soup[id(pd)] = pd_evs[-1]
        return evs
    
    def updateFromCalculator(self, calc):
        changed = []
        for defn in self.defn_for.values():
            if isinstance(defn, _LeafDefn):
                defn.updateFromCalculator(calc)
                changed.append(defn)
        self.update(changed)
    
    def getNumFreeParams(self):
        return sum(defn.getNumFreeParams() for defn in self.defns if isinstance(defn, _LeafDefn))
    

class CalculationDefn(_NonLeafDefn):
    """Defn for a derived value.  In most cases use CalcDefn instead
    
    The only reason for subclassing this directly would be to override
    .makeCalcFunction() or setup()."""
    
    recycling = False
    
    # positional arguments are inputs to this step of the calculation,
    # keyword arguments are passed on to self.setup(), likely to end up
    # as static attributes of this CalculationDefn, to be used (as self.X)
    # by its 'calc' method.
    
    def makeParamController(self):
        return ParameterController(self)
    
    def setup(self):
        pass
    
    def makeCalcFunction(self):
        return self.calc
    
    def makeEvaluators(self, input_soup, variable=None):
        # input soups contains all necessary values for calc on self. Going from defns to cells.
        cells = []
        for input_nums in self.uniq:
            args = []
            all_const = True
            for (arg, u) in zip(self.args, input_nums):
                ev = input_soup[id(arg)]
                arg = ev.outputs[u]
                args.append(arg)
                all_const = all_const and arg.is_const
            calc = self.makeCalcFunction()
            cell = EvaluatedCell(self.name, calc, args,
                    recycling=self.recycling, default=self.default)
            cells.append(cell)
        return [Evaluator(self, cells, cells)]
    

class _FuncDefn(CalculationDefn):
    def __init__(self, calc, name, *args):
        self.calc = calc
        CalculationDefn.__init__(self, *args, **{'name':name})
    

# Use this rather than having to subclass CalculationDefinition
# just to supply the 'calc' method.
class CalcDefn(object):
    """CalcDefn(function)(arg1, arg2)"""
    def __init__(self, calc, name=None, **kw):
        self.calc = calc
        
        if name is not None:
            assert isinstance(name, basestring), name
            self.name = name
        
        if not getattr(self, 'name', None):
            if hasattr(self.calc, '__name__'):
                self.name = self.calc.__name__
        
        self.kw = kw
    
    def __call__(self, *args):
        return _FuncDefn(self.calc, self.name, *args)

class WeightedPartitionDefn(CalculationDefn):
    """Uses a PartitionDefn (ie: N-1 optimiser parameters) to make
    an array of floats with weighted average of 1.0"""
    
    def __init__(self, weights, name):
        N = len(weights.bin_names)
        partition = PartitionDefn(size=N, name=name+'_partition')
        partition.user_param = False
        CalculationDefn.__init__(self, weights, partition,
                name=name+'_distrib')
    
    def calc(self, weights, values):
        scale = numpy.sum(weights * values)
        return values / scale
    

class MonotonicDefn(WeightedPartitionDefn):
    """Uses a PartitionDefn (ie: N-1 optimiser parameters) to make
    an ordered array of floats with weighted average of 1.0"""
    
    def calc(self, weights, increments):
        values = numpy.add.accumulate(increments)
        scale = numpy.sum(weights * values)
        return values / scale
    

class GammaDefn(MonotonicDefn):
    """Uses 1 optimiser parameter to define a gamma distribution, divides
    the distribution into N equal probability bins and makes an array of
    their medians.  If N > 2 medians are approx means so their average
    is approx 1.0, but not quite so we scale them to make it exactly 1.0"""
    
    name = 'gamma'
    
    def __init__(self, weights, name=None, default_shape=1.0,
            extra_label=None, dimensions=()):
        name = self.makeName(name, extra_label)
        shape = PositiveParamDefn(name+'_shape',
            default=default_shape, dimensions=dimensions, lower=1e-2)
        CalculationDefn.__init__(self, weights, shape, name=name+'_distrib')
    
    def calc(self, weights, a):
        from cogent.maths.stats.distribution import gdtri
        weights = weights / numpy.sum(weights)
        percentiles = (numpy.add.accumulate(weights) - weights*0.5)
        medians = numpy.array([gdtri(a,a,p) for p in percentiles])
        scale = numpy.sum(medians*weights)
        #assert 0.5 < scale < 2.0, scale # medians as approx. to means.
        return medians / scale


class _InputDefn(_LeafDefn):
    user_param = True
    
    def __init__(self, name=None, default=None, dimensions=None,
            lower=None, upper=None, **kw):
        _LeafDefn.__init__(self, name=name, dimensions=dimensions, **kw)
        if default is not None:
            if hasattr(default, '__len__'):
                default = numpy.array(default)
            self.default = default
        # these two have no effect on constants
        if lower is not None:
            self.lower = lower
        if upper is not None:
            self.upper = upper
    
    def makeParamController(self):
        return ParameterController(self)
    
    def updateFromCalculator(self, calc):
        ev = calc.evs_by_name[self.name]
        for (cell, setting) in zip(ev.outputs, self.uniq):
            setting.value = calc._getCurrentCellValue(cell)
    
    def getNumFreeParams(self):
        return sum(len([c for c in ev.cells if isinstance(c, OptPar)])
                for ev in self.makeEvaluators({}, None))
    

class ParamDefn(_InputDefn):
    """Defn for an optimisable, scalar input to the calculation"""
    
    numeric = True
    const_by_default = False
    independent_by_default = False
    opt_par_class = OptPar
    
    # These can be overridden in a subclass or the constructor
    default = 1.0
    lower = -1e10
    upper = +1e10
    
    def makeDefaultSetting(self):
        return Var(bounds = (self.lower, self.default, self.upper))
    
    def checkSettingIsValid(self, setting):
        pass
    
    def makeEvaluators(self, input_soup={}, variable=None):
        uniq_cells = []
        partitions = {}
        for (i, v) in enumerate(self.uniq):
            scope = [key for key in self.assignments
                    if self.assignments[key] is v]
            if v.is_const or (variable is not None and variable is not v):
                cell = ConstCell(self.name, v.value)
            elif isinstance(v, Part):
                key = id(v.whole)
                if key not in partitions:
                    partitions[key] = (v.whole, [])
                partitions[key][-1].append((i, v.value))
                cell = None # placeholder, see below
            else:
                cell = self.opt_par_class(self.name, scope, v.getBounds())
            uniq_cells.append(cell)
        
        # A partition is used when two adjacent edges have a constant
        # total length
        private_evs = []
        for (whole, parts) in partitions.values():
            # Hack. 1-scope. No PC involved
            values = numpy.array([v for (i,v) in parts])
            sub_evs = whole.makePrivateEvaluators(default=values)
            private_evs.extend(sub_evs)
            partition = theOneItemIn(sub_evs[-1].outputs)
            
            for (p, (i,v)) in enumerate(parts):
                assert uniq_cells[i] is None, uniq_cells[i]
                uniq_cells[i] = EvaluatedCell(
                        self.name, (lambda x,p=p:x[p]), (partition,))
        return private_evs + [Evaluator(self, uniq_cells, uniq_cells)]
    

# Example / basic ParamDefn subclasses

class PositiveParamDefn(ParamDefn):
    lower = 0.0

class ProbabilityParamDefn(PositiveParamDefn):
    upper = 1.0

class RatioParamDefn(PositiveParamDefn):
    lower = 1e-6
    upper = 1e+6
    opt_par_class = LogOptPar

class NonScalarDefn(_InputDefn):
    """Defn for an array or other such object that is an input but
    can not be optimised"""
    
    user_param = False
    numeric = False
    const_by_default = True
    independent_by_default = False
    default = None
    
    def makeDefaultSetting(self):
        if self.default is None:
            return None
        else:
            return ConstVal(self.default)
    
    def checkSettingIsValid(self, setting):
        if not isinstance(setting, ConstVal):
            raise ValueError("%s can only be constant" % self.name)
    
    def makeEvaluators(self, input_soup={}, variable=None):
        if None in self.uniq:
            if [v for v in self.uniq if v is not None]:
                scope = [key for key in self.assignments
                            if self.assignments[key] is None]
                msg = 'Unoptimisable input "%%s" not set for %s' % scope
            else:
                msg = 'Unoptimisable input "%s" not given'
            raise ValueError(msg % self.name)
        uniq_cells = [ConstCell(self.name, v.value) for v in self.uniq]
        return [Evaluator(self, uniq_cells, uniq_cells)]
    
    def getNumFreeParams(self):
        return 0
    

def _proportions(total, params):
    """List of N proportions from N-1 ratios
    
    >>> _proportions(1.0, [3, 1, 1])
    [0.125, 0.125, 0.375, 0.375]"""
    if len(params) == 0:
        return [total]
    half = (len(params)+1) // 2
    part = 1.0 / (params[0] + 1.0) # ratio -> proportion
    return _proportions(total*part, params[1:half]) + \
        _proportions(total*(1.0-part), params[half:])

def _unpack_proportions(values):
    """List of N-1 ratios from N proportions"""
    if len(values) == 1:
        return []
    half = len(values) // 2
    ratio = sum(values[half:]) / sum(values[:half])
    return [ratio] + _unpack_proportions(values[:half]) + \
        _unpack_proportions(values[half:])

class PartitionDefn(_InputDefn):
    """A partition such as mprobs can be const or optimised.  Optimised is
    a bit tricky since it isn't just a scalar."""
    
    numeric = False # well, not scalar anyway
    const_by_default = False
    independent_by_default = False
    
    def __init__(self, default=None, name=None, dimensions=None,
            dimension=None, size=None, **kw):
        assert name
        if size is not None:
            pass
        elif default is not None:
            size = len(default)
        elif dimension is not None:
            size = len(dimension[1])
        if dimension is not None:
            self.internal_dimension = dimension
            (dim_name, dim_cats) = dimension
            self.bin_names = dim_cats
            self.array_template = DictArrayTemplate(dim_cats)
            self.internal_dimensions = (dim_name,)
        if default is None:
            default = numpy.array([1.0/size] * size)
        _InputDefn.__init__(self, name=name, default=default,
            dimensions=dimensions, **kw)
        self.size = size
    
    def checkSettingIsValid(self, setting):
        assert setting.getDefaultValue().shape == (self.size,), setting
        assert abs(sum(setting.getDefaultValue()) - 1.0) < .00001
    
    def makeDefaultSetting(self):
        #return ConstVal(self.default)
        return Var((None, self.default.copy(), None))
    
    def _makePartitionCell(self, name, scope, value):
        # This is in its own function so as to provide a closure containing
        # the correct value of 'total'
        # for calc's involving pars that need to sum to amount,
        # like a root branch
        N = len(value)
        total = sum(value)
        ratios = _unpack_proportions(value)
        ratios = [LogOptPar(name+'_ratio', scope, (1e-6,r,1e+6))
                for r in ratios]
        def r2p(*ratios):
            return numpy.asarray(_proportions(total, ratios))
        partition = EvaluatedCell(name, r2p, tuple(ratios))
        return (ratios, partition)
    
    def makeEvaluators(self, input_soup={}, variable=None):
        uniq_cells = []
        all_cells = []
        for (i, v) in enumerate(self.uniq):
            if v is None:
                raise ValueError("input %s not set" % self.name)
            assert hasattr(v, 'getDefaultValue'), v
            value = v.getDefaultValue()
            assert hasattr(value, 'shape'), value
            assert value.shape == (self.size,)
            scope = [key for key in self.assignments
                    if self.assignments[key] is v]
            assert value is not None
            if v.is_const or (variable is not None and variable is not v):
                partition = ConstCell(self.name, value)
            else:
                (ratios, partition) = self._makePartitionCell(
                        self.name, scope, value)
                all_cells.extend(ratios)
            all_cells.append(partition)
            uniq_cells.append(partition)
        return [Evaluator(self, all_cells, uniq_cells)]
    

def NonParamDefn(name, dimensions=None):
    # Just to get 2nd arg as dimensions
    return NonScalarDefn(name=name, dimensions=dimensions)

class ConstDefn(NonScalarDefn):
    # This isn't really needed - just use NonParamDefn
    name_required = False
    
    def __init__(self, value, name=None, **kw):
        NonScalarDefn.__init__(self, default=value, name=name, **kw)
    
    def checkSettingIsValid(self, setting):
        if setting is not None and setting.value is not self.default:
            raise ValueError("%s is constant" % self.name)
    
    def updateFromCalculator(self, calc):
        pass
    

class SelectForDimension(_Defn):
    """A special kind of Defn used to bridge from Defns where a particular
    dimension is wrapped up inside an array to later Defns where each
    value has its own Defn, eg: gamma distributed rates"""
    
    name = 'select'
    user_param = True
    numeric=True # not guarenteed!
    internal_dimensions = ()
    
    def __init__(self, arg, dimension, name=None):
        assert not arg.activated, arg.name
        if name is not None:
            self.name = name
        _Defn.__init__(self)
        self.args = (arg,)
        self.arg = arg
        self.valid_dimensions = arg.valid_dimensions
        if dimension not in self.valid_dimensions:
            self.valid_dimensions =  self.valid_dimensions + (dimension,)
        self.dimension = dimension
        arg.addClient(self)
    
    def update(self):
        for scope_t in self.assignments:
            scope = dict(zip(self.valid_dimensions, scope_t))
            scope2 = dict((n,v) for (n,v) in scope.items() if n!=self.dimension)
            input_num = self.arg.outputOrdinalFor(scope2)
            pos = self.arg.bin_names.index(scope[self.dimension])
            self.assignments[scope_t] = (input_num, pos)
        self._update_from_assignments()
        self.values = [self.arg.values[i][p] for (i,p) in self.uniq]
    
    def _select(self, arg, p):
        return arg[p]
    
    def makeEvaluators(self, input_soup, variable=None):
        cells = []
        outputs = []
        distribs = input_soup[id(self.arg)].outputs
        for (input_num, bin_num) in self.uniq:
            cell = EvaluatedCell(
                self.name, (lambda x,p=bin_num:x[p]), (distribs[input_num],))
            cells.append(cell)
            outputs.append(cell)
        return [Evaluator(self, cells, outputs)]
    

# Some simple CalcDefns

#SumDefn = CalcDefn(lambda *args:sum(args), 'sum')
#ProductDefn = CalcDefn(lambda *args:numpy.product(args), 'product')
#CallDefn = CalcDefn(lambda func,*args:func(*args), 'call')
#ParallelSumDefn = CalcDefn(lambda comm,local:comm.sum(local), 'parallel_sum')

class SumDefn(CalculationDefn):
    name = 'sum'
    def calc(self, *args):
        return sum(args)
    

class ProductDefn(CalculationDefn):
    name = 'product'
    def calc(self, *args):
        return numpy.product(args)
    

class CallDefn(CalculationDefn):
    name = 'call'
    def calc(self, func, *args):
        return func(*args)
    

class ParallelSumDefn(CalculationDefn):
    name = 'parallel_sum'
    def calc(self, comm, local):
        return comm.sum(local)
    

__all__ = ['ConstDefn', 'NonParamDefn', 'CalcDefn', 'SumDefn', 'ProductDefn',
        'CallDefn', 'ParallelSumDefn'] + [
        n for (n,c) in vars().items()
        if (isinstance(c, type) and issubclass(c, _Defn) and n[0] != '_')
        or isinstance(c, CalcDefn)]

