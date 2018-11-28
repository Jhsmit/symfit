from collections import namedtuple, Mapping, OrderedDict, Sequence
import copy
import sys
import warnings
from abc import abstractmethod

import sympy
from sympy.core.relational import Relational
import numpy as np
from scipy.optimize import minimize
from scipy.integrate import odeint
from toposort import toposort

from symfit.core.argument import Parameter, Variable
from .support import (
    seperate_symbols, keywordonly, sympy_to_py, key2str, partial,
    cached_property, D
)
from .minimizers import (
    BFGS, SLSQP, LBFGSB, BaseMinimizer, GradientMinimizer, ConstrainedMinimizer,
    ScipyMinimize, MINPACK, ChainedMinimizer, BasinHopping
)
from .objectives import (
    LeastSquares, BaseObjective, MinimizeModel, VectorLeastSquares,
    LogLikelihood, HessianObjective, HessianObjectiveJacApprox
)
from .fit_results import FitResults

if sys.version_info >= (3,0):
    import inspect as inspect_sig
else:
    import funcsigs as inspect_sig


def variabletuple(typename, variables, *args, **kwargs):
    """
    Create a :func:`namedtuple` using :class:`~sympy.core.argument.Variable`'s
    whoses names will be used as `field_names`.

    The main reason for using this object is the `_asdict()` method: whereas a
    ``namedtuple`` initiates such an :func:`collections.OrderedDict` with the
    ``field_names`` as keys, this object returns a
    :func:`collections.OrderedDict` which immidiatelly has the ``Variable``
    objects as keys.

    Example::

        >>> x = Variable('x')
        >>> Result = variabletuple('Result', [x])
        >>> res = Result(5.0)
        >>> res._asdict()
        OrderedDict((x, 5.0))

    :param typename: Name of the `variabletuple`.
    :param variables: List of :class:`~sympy.core.argument.Variable`, to be used
        as `field_names`
    :param args: See :func:`collections.namedtuple`
    :param kwargs: See :func:`collections.namedtuple`
    :return: Type ``typename``
    """
    def _asdict(self):
        return OrderedDict(zip(variables, self))

    field_names = [var.name for var in variables]
    named = namedtuple(typename, field_names, *args, **kwargs)
    named._asdict = _asdict
    return named


class ModelError(Exception):
    """
    Raised when a problem occurs with a model.
    """
    pass


class BaseModel(Mapping):
    """
    ABC for ``Model``'s. Makes sure models are iterable.
    Models can be initiated from Mappings or Iterables of Expressions,
    or from an expression directly.
    Expressions are not enforced for ducktyping purposes.
    """
    def __init__(self, model):
        """
        Initiate a Model from a dict::

            a = Model({y: x**2})

        Preferred way of initiating ``Model``, since now you know what the dependent variable is called.

        :param model: dict of ``Expr``, where dependent variables are the keys.
        """
        if not isinstance(model, Mapping):
            try:
                iter(model)
            except TypeError:
                # Model is still a scalar model
                model = [model]
            # TODO: this will break upon deprecating the auto-generation of
            # names for Variables. At this time, a DummyVariable object
            # should be introduced to fulfill the same role.
            model = {Variable(): expr for expr in model}
        self._init_from_dict(model)

    def __len__(self):
        """
        :return: the number of dependent variables for this model.
        """
        return len(self.model_dict)

    def __getitem__(self, var):
        """
        Returns the expression belonging to a given dependent variable.

        :param var: Instance of ``Variable``
        :type var: ``Variable``
        :return: The expression belonging to ``var``
        """
        return self.model_dict[var]

    def __iter__(self):
        """
        :return: iterable over self.model_dict
        """
        return iter(self.model_dict)

    def __eq__(self, other):
        """
        ``Model``'s are considered equal when they have the same dependent variables,
        and the same expressions for those dependent variables. The same is defined here
        as passing sympy == for the vars themselves, and as expr1 - expr2 == 0 for the
        expressions. For more info check the `sympy docs <https://github.com/sympy/sympy/wiki/Faq>`_.

        :param other: Instance of ``Model``.
        :return: bool
        """
        if len(self) is not len(other):
            return False
        else:
            for var_1, var_2 in zip(self, other):
                if var_1 != var_2:
                    return False
                else:
                    if not self[var_1].expand() == other[var_2].expand():
                        return False
            else:
                return True

    def __neg__(self):
        """
        :return: new model with opposite sign. Does not change the model in-place,
            but returns a new copy.
        """
        new_model_dict = self.model_dict.copy()
        for key in new_model_dict:
            new_model_dict[key] *= -1
        return self.__class__(new_model_dict)

    def _init_from_dict(self, model_dict):
        """
        Initiate self from a model_dict to make sure attributes such as vars, params are available.

        Creates lists of alphabetically sorted independent vars, dependent vars, sigma vars, and parameters.
        Finally it creates a signature for this model so it can be called nicely. This signature only contains
        independent vars and params, as one would expect.

        :param model_dict: dict of (dependent_var, expression) pairs.
        """
        sort_func = lambda symbol: symbol.name
        self.model_dict = OrderedDict(sorted(model_dict.items(),
                                             key=lambda i: sort_func(i[0])))
        # Everything at the bottom of the toposort is independent, at the top
        # dependent, and the rest interdependent.
        ordered = list(toposort(self.connectivity_mapping))
        independent = sorted(ordered.pop(0), key=sort_func)
        self.dependent_vars = sorted(ordered.pop(-1), key=sort_func)
        self.interdependent_vars = sorted(
            [item for items in ordered for item in items],
            key=sort_func
        )
        # `independent` contains both params and vars, needs to be separated
        self.params = [s for s in independent if isinstance(s, Parameter)]
        self.independent_vars = [s for s in independent
                                 if not isinstance(s, Parameter) and not s in self]

        # Make Variable object corresponding to each depedent var.
        self.sigmas = {var: Variable(name='sigma_{}'.format(var.name))
                       for var in self.dependent_vars}

    @cached_property
    def vars_as_functions(self):
        """
        :return: Turn the keys of this model into :mod:`~sympy.Function`
            objects. This is done recursively so the chain rule can be applied
            correctly. This is done on the basis of `connectivity_mapping`.

            Example: for ``{y: a * x, z: y**2 + a}`` this returns
            ``{y: y(x, a), z: z(y(x, a), a)}``.
        """
        vars2functions = {}
        key = lambda arg: [isinstance(arg, Parameter), str(arg)]
        # Iterate over all symbols in this model in topological order, turning
        # each one into a function object recursively.
        for symbol in self.ordered_symbols:
            if symbol in self.connectivity_mapping:
                dependencies = self.connectivity_mapping[symbol]
                # Replace the dependency by it's function if possible
                dependencies = [vars2functions.get(dependency, dependency)
                               for dependency in dependencies]
                # sort by vars first, then params, and alphabetically within
                # each group
                dependencies = sorted(dependencies, key=key)
                vars2functions[symbol] = sympy.Function(symbol.name)(*dependencies)
        return vars2functions

    @cached_property
    def function_dict(self):
        """
        Equivalent to ``self.model_dict``, but with all variables replaced by
        functions if applicable. Sorted by the evaluation order according to
        ``self.ordered_symbols``, not alphabetical like ``self.model_dict``!
        """
        func_tuples = []
        for var, func in self.vars_as_functions.items():
            expr = self.model_dict[var].xreplace(self.vars_as_functions)
            func_tuples.append((func, expr))
        return OrderedDict(func_tuples)

    @cached_property
    def connectivity_mapping(self):
        """
        :return: This property returns a mapping of the interdepencies between
            variables. This is essentially the dict representation of a
            connectivity graph, because working with this dict results in
            cleaner code. Treats variables and parameters on the same footing.
        """
        connectivity = {}
        for var, expr in self.items():
            vars, params = seperate_symbols(expr)
            connectivity[var] = set(vars + params)
        return connectivity

    @property
    def ordered_symbols(self):
        """
        :return: list of all symbols in this model, topologically sorted so they
            can be evaluated in the correct order.

            Within each group of equal priority symbols, we sort by the order of
            the derivative.
        """
        key_func = lambda s: [isinstance(s, sympy.Derivative),
                           isinstance(s, sympy.Derivative) and s.derivative_count]
        symbols = []
        for symbol in toposort(self.connectivity_mapping):
            symbols.extend(sorted(symbol, key=key_func))

        return symbols

    @cached_property
    def vars(self):
        """
        :return: Returns a list of dependent, independent and sigma variables, in that order.
        """
        return self.independent_vars + self.dependent_vars + [self.sigmas[var] for var in self.dependent_vars]

    @property
    def bounds(self):
        """
        :return: List of tuples of all bounds on parameters.
        """
        bounds = []
        for p in self.params:
            if p.fixed:
                if p.value >= 0.0:
                    bounds.append([np.nextafter(p.value, 0), p.value])
                else:
                    bounds.append([p.value, np.nextafter(p.value, 0)])
            else:
                bounds.append([p.min, p.max])
        return bounds

    @property
    def shared_parameters(self):
        """
        :return: bool, indicating if parameters are shared between the vector
            components of this model.
        """
        if len(self) == 1:  # Not a vector
            return False
        else:
            params_thusfar = []
            for component in self.values():
                vars, params = seperate_symbols(component)
                if set(params).intersection(set(params_thusfar)):
                    return True
                else:
                    params_thusfar += params
            else:
                return False

    @property
    def free_params(self):
        """
        :return: ordered list of the subset of variable params
        """
        return [p for p in self.params if not p.fixed]

    def __str__(self):
        """
        Printable representation of a Mapping model.

        :return: str
        """
        template = "{}({}; {}) = {}"
        parts = []
        for var, expr in self.items():
            # Print every component as a function of only the dependencies it
            # has. We can deduce these from the connectivity mapping.
            params_sorted = sorted((x for x in self.connectivity_mapping[var]
                                    if isinstance(x, Parameter)),
                                   key=lambda x: x.name)
            vars_sorted = sorted((x for x in self.connectivity_mapping[var]
                                  if x not in params_sorted),
                                 key=lambda x: x.name)
            parts.append(template.format(
                    var,
                    ', '.join([x.name for x in vars_sorted]),
                    ', '.join([x.name for x in params_sorted]),
                    expr
                )
            )
        return '[{}]'.format(",\n ".join(parts))

    def __getstate__(self):
        # Remove cached_property values from the state, they need to be
        # re-calculated after pickle.
        state = self.__dict__.copy()
        del state['__signature__']
        for key in self.__dict__:
            if key.startswith(cached_property.base_str):
                del state[key]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__signature__ = self._make_signature()


class BaseNumericalModel(BaseModel):
    """
    ABC for Numerical Models. These are models whose components are generic
    python callables.
    """
    @keywordonly(connectivity_mapping=None)
    def __init__(self, model, independent_vars=None, params=None, **kwargs):
        """
        :param model: dict of ``callable``, where dependent variables are the
            keys. If instead of a dict a (sequence of) ``callable`` is provided,
            it will be turned into a dict automatically.
        :param independent_vars: The independent variables of the  model.
            (Deprecated, use ``connectivity_mapping`` instead.)
        :param params: The parameters of the model.
            (Deprecated, use ``connectivity_mapping`` instead.)
        :param connectivity_mapping: Mapping indicating the dependencies of
            every variable in the model. For example, a model_dict
            {y: a * x + b} has a connectivity_mapping {y: {x, a, b}}. Note that
            the values of this dict have to be sets.
        """
        connectivity_mapping = kwargs.pop('connectivity_mapping')
        if connectivity_mapping is None and \
                independent_vars is not None and params is not None:
            # Make model into a mapping if needed.
            if not isinstance(model, Mapping):
                try:
                    iter(model)
                except TypeError:
                    model = [model]  # make model iterable

                model = {Variable(): expr for expr in model}
            warnings.warn(DeprecationWarning(
                '`independent_vars` and `params` have been deprecated.'
                ' Use `connectivity_mapping` instead.'
            ))
            self.independent_vars = sorted(independent_vars, key=str)
            self.params = sorted(params, key=str)
            self.connectivity_mapping = {var: set(independent_vars + params)
                                         for var in model}
        elif connectivity_mapping:
            self.connectivity_mapping = connectivity_mapping
            if not isinstance(model, Mapping):
                raise TypeError('Please provide the model as a mapping, '
                                'corresponding to `connectivity_mapping`.')
        else:
            raise TypeError('Please provide `connectivity_mapping`.')
        super(BaseNumericalModel, self).__init__(model)

    @property
    def connectivity_mapping(self):
        return self._connectivity_mapping

    @connectivity_mapping.setter
    def connectivity_mapping(self, value):
        self._connectivity_mapping = value

    def __eq__(self, other):
        raise NotImplementedError(
            'Equality checking for {} is ambiguous.'.format(self.__class__.__name__)
        )

    def __neg__(self):
        """
        :return: new model with opposite sign. Does not change the model in-place,
            but returns a new copy.
        """
        new_model_dict = {}
        for key, callable_expr in self.model_dict.values():
            new_model_dict[key] = lambda *args, **kwargs: - callable_expr(*args, **kwargs)
        return self.__class__(new_model_dict)

    @property
    def shared_parameters(self):
        """
        BaseNumericalModel's cannot infer if parameters are shared.
        """
        raise NotImplementedError(
            'Shared parameters can not be inferred for {}'.format(self.__class__.__name__)
        )


class BaseCallableModel(BaseModel):
    """
    Baseclass for callable models. A callable model is expected to have
    implemented a `__call__` method which evaluates the model.
    """
    def eval_components(self, *args, **kwargs):
        """
        :return: evaluated lambda functions of each of the components in
            model_dict, to be used in numerical calculation.
        """
        bound_arguments = self.__signature__.bind(*args, **kwargs)
        kwargs = bound_arguments.arguments  # Only work with kwargs
        components = dict(zip(self, self.numerical_components))
        # Evaluate the variables in topological order.
        for symbol in self.ordered_symbols:
            if symbol.name not in kwargs:
                dependencies = self.connectivity_mapping[symbol]
                dependencies_kwargs = {d.name: kwargs[d.name]
                                       for d in dependencies}
                kwargs[symbol.name] = components[symbol](**dependencies_kwargs)

        return [np.atleast_1d(kwargs[var.name]) for var in self]

    def numerical_components(self):
        """
        :return: A list of callables corresponding to each of the components
            of the model.
        """
        raise NotImplementedError(
            ('No `numerical_components` is defined for object of type {}. '
             'Implement either `numerical_components`, or change '
             '`eval_components` so it no longer calls '
             '`numerical_components`.').format(self.__class__)
        )

    def _make_signature(self):
        # Handle args and kwargs according to the allowed names.
        parameters = [
            # Note that these are inspect_sig.Parameter's, not symfit parameters!
            inspect_sig.Parameter(arg.name,
                                  inspect_sig.Parameter.POSITIONAL_OR_KEYWORD)
            for arg in self.independent_vars + self.params
        ]
        return inspect_sig.Signature(parameters=parameters)

    def _init_from_dict(self, model_dict):
        super(BaseCallableModel, self)._init_from_dict(model_dict)
        self.__signature__ = self._make_signature()

    def __call__(self, *args, **kwargs):
        """
        Evaluate the model for a certain value of the independent vars and parameters.
        Signature for this function contains independent vars and parameters, NOT dependent and sigma vars.

        Can be called with both ordered and named parameters. Order is independent vars first, then parameters.
        Alphabetical order within each group.

        :param args:
        :param kwargs:
        :return: A namedtuple of all the dependent vars evaluated at the desired point. Will always return a tuple,
            even for scalar valued functions. This is done for consistency.
        """
        Ans = variabletuple('Ans', self)
        return Ans(*self.eval_components(*args, **kwargs))


class BaseGradientModel(BaseCallableModel):
    """
    Baseclass for models which have a gradient. Such models are expected to
    implement an `eval_jacobian` function.

    Any subclass of this baseclass which does not implement its own
    `eval_jacobian` will inherit a finite difference gradient.
    """
    @keywordonly(dx=1e-8)
    def finite_difference(self, *args, **kwargs):
        """
        Calculates a numerical approximation of the Jacobian of the model using
        the sixth order central finite difference method. Accepts a `dx`
        keyword to tune the relative stepsize used.
        Makes 6*n_params calls to the model.

        :return: A numerical approximation of the Jacobian of the model as a
                 list with length n_components containing numpy arrays of shape
                 (n_params, n_datapoints)
        """
        # See also: scipy.misc.derivative. It might be convinced to work, but
        # it will make way too many function evaluations
        dx = kwargs.pop('dx')
        bound_arguments = self.__signature__.bind(*args, **kwargs)
        var_vals = [bound_arguments.arguments[var.name] for var in self.independent_vars]
        param_vals = [bound_arguments.arguments[param.name] for param in self.params]
        param_vals = np.array(param_vals, dtype=float)
        f = partial(self, *var_vals)
        # See also: scipy.misc.central_diff_weights
        factors = np.array((3/2., -3/5., 1/10.))
        orders = np.arange(1, len(factors) + 1)
        out = []
        # TODO: Dark numpy magic. Needs an extra dimension in out, and a sum
        #       over the right axis at the end.

        # We can't make the output arrays yet, since we don't know the size of
        # the components. So put a sentinel value.
        out = None

        for param_idx, param_val in enumerate(param_vals):
            for order, factor in zip(orders, factors):
                h = np.zeros(len(self.params))
                # Note: stepsize (h) depends on the parameter values...
                h[param_idx] = dx * order
                if abs(param_val) >= 1e-7:
                    # ...but it'd better not be (too close to) 0.
                    h[param_idx] *= param_val
                up = f(*(param_vals + h))
                down = f(*(param_vals - h))
                if out is None:
                    # Initialize output arrays. Now that we evaluated f, we
                    # know the size of our data.
                    out = []
                    # out is a list  of length Ncomponents with numpy arrays of
                    # shape (Nparams, Ndata). Part of our misery comes from the
                    # fact that the length of the data may be different for all
                    # the components. Numpy doesn't like ragged arrays, so make
                    # a list of arrays.
                    for comp_idx in range(len(self)):
                        try:
                            len(up[comp_idx])
                        except TypeError:  # output[comp_idx] is a number
                            data_shape = (1,)
                        else:
                            data_shape = up[comp_idx].shape
                        # Initialize at 0 so we can += all the contributions
                        param_grad = np.zeros([len(self.params)] + list(data_shape), dtype=float)
                        out.append(param_grad)
                for comp_idx in range(len(self)):
                    diff = up[comp_idx] - down[comp_idx]
                    out[comp_idx][param_idx, :] += factor * diff / (2 * h[param_idx])
        return out

    def eval_jacobian(self, *args, **kwargs):
        """
        :return: The jacobian matrix of the function.
        """
        Ans = variabletuple('Ans', self)
        return Ans(*self.finite_difference(*args, **kwargs))


class CallableNumericalModel(BaseCallableModel, BaseNumericalModel):
    """
    Callable model, whose components are callables provided by the user.
    This allows the user to provide the components directly.

    Example::

        x, y = variables('x, y')
        a, b = parameters('a, b')
        numerical_model = CallableNumericalModel(
            {y: lambda x, a, b: a * x + b},
            independent_vars=[x],
            params=[a, b]
        )

    This is identical in functionality to the more traditional::

        x, y = variables('x, y')
        a, b = parameters('a, b')
        model = CallableModel({y: a * x + b})

    but allows power-users a lot more freedom while still interacting
    seamlessly with the :mod:`symfit` API.

    .. note:: All of the callables must accept all of the ``independent_vars``
        and  ``params`` of the model as arguments, even if not all of them are
        used by every callable.
    """
    @cached_property
    def numerical_components(self):
        return [expr if not isinstance(expr, sympy.Expr) else
                sympy_to_py(expr, self.connectivity_mapping[var], [])
                for var, expr in self.items()]


class CallableModel(BaseCallableModel):
    """
    Defines a callable model. The usual rules apply to the ordering of the
    arguments:

    * first independent variables, then dependent variables, then parameters.
    * within each of these groups they are ordered alphabetically.
    """
    @cached_property
    def numerical_components(self):
        """
        :return: lambda functions of each of the analytical components in
            model_dict, to be used in numerical calculation.
        """
        Ans = variabletuple('Ans', self.keys())
        # All components must feature the independent vars and params, that's
        # the API convention. But for those components which also contain
        # interdependence, we add those vars
        components = []
        for var, expr in self.items():
            dependencies = self.connectivity_mapping[var]
            # vars first, then params, and alphabetically within each group
            key = lambda arg: [isinstance(arg, Parameter), str(arg)]
            ordered = sorted(dependencies, key=key)
            components.append(sympy_to_py(expr, ordered, []))
        return Ans(*components)


class GradientModel(CallableModel, BaseGradientModel):
    """
    Analytical model which has an analytically computed Jacobian.
    """
    def __init__(self, *args, **kwargs):
        super(GradientModel, self).__init__(*args, **kwargs)
        self.jacobian_model = jacobian_from_model(self)

    @cached_property
    def jacobian(self):
        """
        :return: Jacobian filled with the symbolic expressions for all the
            partial derivatives. Partial derivatives are of the components of
            the function with respect to the Parameter's, not the independent
            Variable's. The return shape is a list over the models components,
            filled with tha symbolical jacobian for that component, as a list.
        """
        jac = []
        for var, expr in self.items():
            jac_row = []
            for param in self.params:
                partial_dv = D(var, param)
                jac_row.append(self.jacobian_model[partial_dv])
            jac.append(jac_row)
        return jac

    def eval_jacobian(self, *args, **kwargs):
        """
        :return: Jacobian evaluated at the specified point.
        """
        eval_jac_dict = self.jacobian_model(*args, **kwargs)._asdict()
        # Take zero for component which are not present, happens for Constraints
        jac = [[eval_jac_dict.get(D(var, param), 0) * np.ones_like(eval_jac_dict[var])
                for param in self.params]
            for var in self
        ]

        # Use numpy to broadcast these arrays together and then stack them along
        # the parameter dimension. We do not include the component direction in
        # this, because the components can have independent shapes.
        for idx, comp in enumerate(jac):
            jac[idx] = np.stack(np.broadcast_arrays(*comp))

        Ans = variabletuple('Ans', self.keys())
        return Ans(*jac)

class HessianModel(GradientModel):
    """
    Analytical model which has an analytically computed Hessian.
    """
    def __init__(self, *args, **kwargs):
        super(HessianModel, self).__init__(*args, **kwargs)
        self.hessian_model = hessian_from_model(self)

    @property
    def hessian(self):
        """
        :return: Hessian filled with the symbolic expressions for all the
            second order partial derivatives. Partial derivatives are taken with
            respect to the Parameter's, not the independent Variable's.
        """
        return [[[sympy.diff(partial_dv, param) for param in self.params]
                 for partial_dv in comp] for comp in self.jacobian]

    def eval_hessian(self, *args, **kwargs):
        """
        :return: Hessian evaluated at the specified point.
        """
        # Evaluate the hessian model and use the resulting Ans namedtuple as a
        # dict. From this, take the relevant components.
        eval_hess_dict = self.hessian_model(*args, **kwargs)._asdict()
        hess = [[[eval_hess_dict.get(D(var, p1, p2), 0) * np.ones_like(eval_hess_dict[var])
                    for p2 in self.params]
                for p1 in self.params]
            for var in self
        ]
        # Use numpy to broadcast these arrays together and then stack them along
        # the parameter dimension. We do not include the component direction in
        # this, because the components can have independent shapes.
        for idx, comp in enumerate(hess):
            hess[idx] = np.stack(np.broadcast_arrays(*comp))

        Ans = variabletuple('Ans', self.keys())
        return Ans(*hess)


class Model(HessianModel):
    """
    Model represents a symbolic function and all it's derived properties such as
    sum of squares, jacobian etc.
    Models should be initiated from a dict::

        a = Model({y: x**2})

    Models are callable. The usual rules apply to the ordering of the arguments:

    * first independent variables, then parameters.
    * within each of these groups they are ordered alphabetically.

    The output of a call to a model is a special kind of namedtuple::

        >>> a(x=3)
        Ans(y=9)

    When turning this into a dict, however, the dict keys will be Variable
    objects, not strings::

        >>> a(x=3)._asdict()
        OrderedDict(((y, 9),))

    Models are also iterable, behaving as their internal model_dict. For
    example, ``a[y]`` returns ``x**2``, ``len(a) == 1``,
    ``y in a == True``, etc.
    """


class Constraint(Model):
    """
    Constraints are a special type of model in that they have a type: >=, == etc.
    They are made to have lhs - rhs == 0 of the original expression.

    For example, Eq(y + x, 4) -> Eq(y + x - 4, 0)

    Since a constraint belongs to a certain model, it has to be initiated with knowledge of it's parent model.
    This is important because all ``numerical_`` methods are done w.r.t. the parameters and variables of the parent
    model, not the constraint! This is because the constraint might not have all the parameter or variables that the
    model has, but in order to compute for example the Jacobian we still want to derive w.r.t. all the parameters,
    not just those present in the constraint.
    """
    @keywordonly(params=None)
    def __init__(self, constraint, model=None, **kwargs):
        """
        :param constraint: constraint that model should be subjected to.
        :param model: A constraint needs to be initiated with all the parameters
            of the corresponiding model, either by providing the model or the
            parameters.
        :param params: list of parameters in case no ``model`` is provided.
        """
        params = kwargs.pop('params')
        if isinstance(constraint, Relational):
            super(Constraint, self).__init__(constraint.lhs - constraint.rhs)
            self.constraint_type = type(constraint)
            if model is None and params is not None:
                self.params = params
            elif isinstance(model, BaseModel):
                self.model = model
                self.params = self.model.params
            else:
                raise TypeError('The model argument must be of type Model.')

            # Update the signature now that self.params has been updated.
            self.__signature__ = self._make_signature()
            # Update the jacobian and hessian model as well
            self.jacobian_model.params = self.params
            self.hessian_model.params = self.params
            self.jacobian_model.__signature__ = self.jacobian_model._make_signature()
            self.hessian_model.__signature__ = self.hessian_model._make_signature()
        else:
            raise TypeError('Constraints have to be initiated with a subclass of sympy.Relational')

    def __neg__(self):
        """
        :return: new model with opposite sign. Does not change the model in-place,
            but returns a new copy.
        """
        new_constraint = self.constraint_type( - self.model_dict[self.dependent_vars[0]])
        return self.__class__(new_constraint, self.model)


class TakesData(object):
    """
    An base class for everything that takes data. Most importantly, it takes care
    of linking the provided data to variables. The allowed variables are extracted
    from the model.
    """
    @keywordonly(absolute_sigma=None)
    def __init__(self, model, *ordered_data, **named_data):
        """
        :param model: (dict of) sympy expression or ``Model`` object.
        :param bool absolute_sigma: True by default. If the sigma is only used
            for relative weights in your problem, you could consider setting it to
            False, but if your sigma are measurement errors, keep it at True.
            Note that curve_fit has this set to False by default, which is wrong in
            experimental science.
        :param ordered_data: data for dependent, independent and sigma variables. Assigned in
            the following order: independent vars are assigned first, then dependent
            vars, then sigma's in dependent vars. Within each group they are assigned in
            alphabetical order.
        :param named_data: assign dependent, independent and sigma variables data by name.

        Standard deviation can be provided to any variable. They have to be prefixed
        with sigma\_. For example, let x be a Variable. Then sigma_x will give the
        stdev in x.
        """
        absolute_sigma = named_data.pop('absolute_sigma')
        if isinstance(model, BaseModel):
            self.model = model
        else:
            self.model = Model(model)

        # Handle ordered_data and named_data according to the allowed names.
        var_names = [var.name for var in self.model.vars]
        parameters = [  # Note that these are inspect_sig.Parameter's, not symfit parameters!
            # inspect_sig.Parameter(name, inspect_sig.Parameter.POSITIONAL_OR_KEYWORD, default=1 if name.startswith('sigma_') else None)
            inspect_sig.Parameter(name, inspect_sig.Parameter.POSITIONAL_OR_KEYWORD, default=None)
                for name in var_names
        ]

        signature = inspect_sig.Signature(parameters=parameters)
        try:
            bound_arguments = signature.bind(*ordered_data, **named_data)
        except TypeError as err:
            for var in self.model.vars:
                if var.name.startswith(Variable._argument_name):
                    raise type(err)(str(err) + '. Some of your Variable\'s are unnamed. That might be the cause of this Error: make sure you use e.g. x = Variable(\'x\')')
            else:
                raise err
        # Include default values in bound_argument object
        for param in signature.parameters.values():
            if param.name not in bound_arguments.arguments:
                bound_arguments.arguments[param.name] = param.default

        original_data = bound_arguments.arguments   # ordereddict of the data
        self.data = OrderedDict((var, original_data[var.name]) for var in self.model.vars)
        self.data.update({var: None for var in self.model.interdependent_vars})
        # Change the type to array if no array operations are supported.
        # We don't want to break duck-typing, hence the try-except.
        for var, dataset in self.data.items():
            try:
                dataset**2
            except TypeError:
                if dataset is not None:
                    self.data[var] = np.array(dataset)
        self.sigmas_provided = any(value is not None for value in self.sigma_data.values())

        # Replace sigmas that are constant by an array of that constant
        for var, sigma in self.model.sigmas.items():
            try:
                iter(self.data[sigma])
            except TypeError:
                if self.data[var] is None and self.data[sigma] is None:
                    if len(self.data_shapes[1]) == 1:
                        # The shape of the dependent vars is unique across dependent vars.
                        # This means we can just assume this shape.
                        self.data[sigma] = np.ones(self.data_shapes[1][0])
                    else: pass # No stdevs can be calculated
                if self.data[var] is not None and self.data[sigma] is None:
                    self.data[sigma] = np.ones(self.data[var].shape)
                elif self.data[var] is not None:
                    self.data[sigma] *= np.ones(self.data[var].shape)

        # If user gives a preference, use that. Otherwise, use True if at least one sigma is
        # given, False if no sigma is given.
        if absolute_sigma is not None:
            self.absolute_sigma = absolute_sigma
        else:
            for sigma in self.sigma_data:
                # Check if the user provided sigmas in the original data.
                # If so, interpret sigmas as measurement errors
                if original_data[sigma.name] is not None:
                    self.absolute_sigma = True
                    break
            else:
                self.absolute_sigma = False

    @property
    def dependent_data(self):
        """
        Read-only Property

        :return: Data belonging to each dependent variable as a dict with
                 variable names as key, data as value.
        :rtype: collections.OrderedDict
        """
        return OrderedDict((var, self.data[var])
                           for var in self.model.dependent_vars)

    @property
    def independent_data(self):
        """
        Read-only Property

        :return: Data belonging to each independent variable as a dict with
                 variable names as key, data as value.
        :rtype: collections.OrderedDict
        """
        return OrderedDict((var, self.data[var]) for var in self.model.independent_vars)

    @property
    def sigma_data(self):
        """
        Read-only Property

        :return: Data belonging to each sigma variable as a dict with
                 variable names as key, data as value.
        :rtype: collections.OrderedDict
        """
        sigmas = self.model.sigmas
        return OrderedDict((sigmas[var], self.data[sigmas[var]])
                           for var in self.model.dependent_vars)

    @property
    def data_shapes(self):
        """
        Returns the shape of the data. In most cases this will be the same for
        all variables of the same type, if not this raises an Exception.

        Ignores variables which are set to None by design so we know that those
        None variables can be assumed to have the same shape as the other in
        calculations where this is needed, such as the covariance matrix.

        :return: Tuple of all independent var shapes, dependent var shapes.
        """
        independent_shapes = []
        for var, data in self.independent_data.items():
            if data is not None:
                independent_shapes.append(data.shape)

        dependent_shapes = []
        for var, data in self.dependent_data.items():
            if data is not None:
                dependent_shapes.append(data.shape)

        return list(set(independent_shapes)), list(set(dependent_shapes))

    @property
    def initial_guesses(self):
        """
        :return: Initial guesses for every parameter.
        """
        return np.array([param.value for param in self.model.params])


class BaseFit(TakesData):
    """
    Abstract base class for all fitting objects.
    """
    def execute(self, *args, **kwargs):
        """
        Every fit object has to define an execute method.
        Any * and ** arguments will be passed to the fitting module that is being wrapped, e.g. leastsq.

        :args kwargs:
        :return: Instance of FitResults
        """
        raise NotImplementedError('Every subclass of BaseFit must have an execute method.')

    def error_func(self, *args, **kwargs):
        """
        Every fit object has to define an error_func method, giving the function to be minimized.
        """
        raise NotImplementedError('Every subclass of BaseFit must have an error_func method.')

    def eval_jacobian(self, *args, **kwargs):
        """
        Every fit object has to define an eval_jacobian method, giving the jacobian of the
        function to be minimized.
        """
        raise NotImplementedError('Every subclass of BaseFit must have an eval_jacobian method.')


class HasCovarianceMatrix(TakesData):
    """
    Mixin class for calculating the covariance matrix for any model that has a
    well-defined Jacobian :math:`J`. The covariance is then approximated as
    :math:`J^T W J`, where W contains the weights of each data point.

    Supports vector valued models, but is unable to estimate covariances for
    those, just variances. Therefore, take the result with a grain of salt for
    vector models.
    """
    def _covariance_matrix(self, best_fit_params, objective):
        # Helper function for self.covariance_matrix.
        try:
            hess = objective.eval_hessian(**key2str(best_fit_params))
        except AttributeError:
            # Some models do not have an eval_jacobian, in which case we give up
            return None

        try:
            hess_inv = np.linalg.inv(hess)
        except np.linalg.LinAlgError:
            return None

        if isinstance(objective, LogLikelihood):
            # Loglikelihood is a special case that needs to be considered
            # separately, see #138.
            return hess_inv
        elif isinstance(objective, LeastSquares):
            # Calculate the covariance for a least squares method.
            # https://www8.cs.umu.se/kurser/5DA001/HT07/lectures/lsq-handouts.pdf
            # Residual sum of squares
            rss = objective(**key2str(best_fit_params))
            # Degrees of freedom
            raw_dof = np.sum([np.product(shape) for shape in self.data_shapes[1]])
            dof = raw_dof - len(self.model.params)
            if self.absolute_sigma:
                # When interpreting as measurement error, we do not rescale.
                s2 = 1
            else:
                s2 = rss / dof
            # Divide by two because the source uses different normalization
            cov_mat = 2 * s2 * hess_inv
            return cov_mat
        else:
            # We do not know how to handle with this situation.
            return None

    def covariance_matrix(self, best_fit_params):
        """
        Given best fit parameters, this function finds the covariance matrix.
        This matrix gives the (co)variance in the parameters.

        :param best_fit_params: ``dict`` of best fit parameters as given by .best_fit_params()
        :return: covariance matrix.
        """
        cov_matrix = self._covariance_matrix(best_fit_params,
                                             objective=self.objective)
        if cov_matrix is None:
            # If the covariance matrix could not be computed, we try again by
            # approximating the hessian with the jacobian.
            if not isinstance(self.objective, HessianObjective) \
                    or not isinstance(self.model, HessianModel):
                # VectorLeastSquares should be turned into a LeastSquares for
                # cov matrix calculation
                if self.objective.__class__ is VectorLeastSquares:
                    base = LeastSquares
                else:
                    base = self.objective.__class__

                class HessApproximation(base, HessianObjectiveJacApprox):
                    """
                    Class which impersonates ``base``, but which returns zeros
                    for the models Hessian. This will effectively result in the
                    calculation of the approximate Hessian by calculating
                    outer(J.T, J) when calling ``base.eval_hessian``.
                    """

                objective = HessApproximation(self.objective.model,
                                              self.objective.data)
                cov_matrix = self._covariance_matrix(best_fit_params,
                                                     objective=objective)

        return cov_matrix


class Fit(HasCovarianceMatrix):
    """
    Your one stop fitting solution! Based on the nature of the input, this
    object will attempt to select the right fitting type for your problem.

    If you need very specific control over how the problem is solved, you can
    pass it the minimizer or objective function you would like to use.

    Example usage::

        a, b = parameters('a, b')
        x, y = variables('x, y')

        model = {y: a * x + b}

        # Fit will use its default settings
        fit = Fit(model, x=xdata, y=ydata)
        fit_result = fit.execute()

        # Use Nelder-Mead instead
        fit = Fit(model, x=xdata, y=ydata, minimizer=NelderMead)
        fit_result = fit.execute()

        # Use Nelder-Mead to get close, and BFGS to polish it off
        fit = Fit(model, x=xdata, y=ydata, minimizer=[NelderMead, BFGS])
        fit_result = fit.execute(minimizer_kwargs=[dict(xatol=0.1), {}])
    """

    @keywordonly(objective=None, minimizer=None, constraints=None)
    def __init__(self, model, *ordered_data, **named_data):
        """

        :param model: (dict of) sympy expression(s) or ``Model`` object.
        :param constraints: iterable of ``Relation`` objects to be used as
            constraints.
        :param bool absolute_sigma: True by default. If the sigma is only used
            for relative weights in your problem, you could consider setting it
            to False, but if your sigma are measurement errors, keep it at True.
            Note that curve_fit has this set to False by default, which is
            wrong in experimental science.
        :param objective: Have Fit use your specified objective. Can be one of
            the predefined `symfit` objectives or any callable which accepts fit
            parameters and returns a scalar.
        :param minimizer: Have Fit use your specified
            :class:`symfit.core.minimizers.BaseMinimizer`. Can be a
            :class:`~collections.abc.Sequence` of :class:`symfit.core.minimizers.BaseMinimizer`.
        :param ordered_data: data for dependent, independent and sigma
            variables. Assigned in the following order: independent vars are
            assigned first, then dependent vars, then sigma's in dependent
            vars. Within each group they are assigned in alphabetical order.
        :param named_data: assign dependent, independent and sigma variables
            data by name.
        """
        objective = named_data.pop('objective')
        minimizer = named_data.pop('minimizer')
        constraints = named_data.pop('constraints')
        super(Fit, self).__init__(model, *ordered_data, **named_data)

        # List of Constraint objects
        self.constraints = self._init_constraints(constraints=constraints)

        if objective is None:
            if minimizer is MINPACK:
                # MINPACK is considered a special snowflake, as its API has to be
                # considered seperately and has its own non standard objective function.
                objective = VectorLeastSquares
            # Param only scalar Model -> the model is the objective.
            elif len(self.model.independent_vars) == 0 and len(self.model) == 1:
                # No data provided means a simple minimization of the Model parameters
                # is requested, not a fit.
                if all(value is None for value in self.data.values()):
                    objective = MinimizeModel

        if objective is None:
            objective = LeastSquares
        elif objective == LogLikelihood or isinstance(objective, LogLikelihood):
            if self.sigmas_provided:
                raise NotImplementedError(
                    'LogLikelihood fitting does not currently support data '
                    'weights.'
                )
        # Initialise the objective if it's not initialised already
        if isinstance(objective, BaseObjective):
            self.objective = objective
        else:
            self.objective = objective(self.model, self.data)

        # Select the minimizer on the basis of the provided information.
        if minimizer is None:
            minimizer = self._determine_minimizer()

        # Initialise the minimizer
        if isinstance(minimizer, Sequence):
            minimizers = [self._init_minimizer(mini) for mini in minimizer]
            self.minimizer = self._init_minimizer(ChainedMinimizer, minimizers=minimizers)
        else:
            self.minimizer = self._init_minimizer(minimizer)

    def _determine_minimizer(self):
        """
        Determine the most suitable minimizer by the presence of bounds or
        constraints.
        :return: a subclass of `BaseMinimizer`.
        """
        if self.constraints:
            return SLSQP
        elif any([bound is not None for pair in self.model.bounds for bound in pair]):
            # If any bound is set
            return LBFGSB
        else:
            return BFGS

    def _init_minimizer(self, minimizer, **minimizer_options):
        """
        Takes a :class:`~symfit.core.minimizers.BaseMinimizer` and instantiates
        it, passing the jacobian and constraints as appropriate for the
        minimizer.

        :param minimizer: :class:`~symfit.core.minimizers.BaseMinimizer` to
            instantiate.
        :param **minimizer_options: Further options to be passed to the
            minimizer on instantiation.
        :returns: instance of :class:`~symfit.core.minimizers.BaseMinimizer`.
        """

        if isinstance(minimizer, BaseMinimizer):
            return minimizer
        if issubclass(minimizer, BasinHopping):
            minimizer_options['local_minimizer'] = self._init_minimizer(
                self._determine_minimizer()
            )
        if issubclass(minimizer, GradientMinimizer):
            # If an analytical version of the Jacobian exists we should use
            # that, otherwise we let the minimizer estimate it itself.
            # Hence the check of numerical_jacobian, as this is the
            # py function version of the analytical jacobian.
            if hasattr(self.model, 'eval_jacobian') and hasattr(self.objective, 'eval_jacobian'):
                minimizer_options['jacobian'] = self.objective.eval_jacobian

        if issubclass(minimizer, ConstrainedMinimizer):
            # set the constraints as MinimizeModel. The dependent vars of the
            # constraint are set to None since their value is irrelevant.
            constraint_objectives = []
            for constraint in self.constraints:
                data = self.data  # No copy, share state
                data.update({var: None for var in constraint.dependent_vars})
                constraint_objectives.append(MinimizeModel(constraint, data))
            minimizer_options['constraints'] = constraint_objectives
        return minimizer(self.objective, self.model.params, **minimizer_options)

    def _init_constraints(self, constraints):
        """
        Takes the user provided constraints and converts them to a list of
        :class:`~symfit.core.fit.Constraint` objects.

        :param constraints: iterable of :class:`~sympy.core.relational.Relation`
            objects.
        :return: list of :class:`~symfit.core.fit.Constraint` objects.
        """
        con_models = []
        if constraints:
            for constraint in constraints:
                if isinstance(constraint, Constraint):
                    con_models.append(constraint)
                else:
                    con_models.append(Constraint(constraint, self.model))
            # Check if the type of each constraint is allowed.
            allowed_types = [sympy.Eq, sympy.Ge, sympy.Le]
            for index in range(len(con_models)):
                constraint = con_models[index]
                if constraint.constraint_type not in allowed_types:
                    raise ModelError(
                        'Only constraints of the type {} are allowed. A constraint'
                        ' of type {} was provided.'.format(allowed_types,
                                                           constraint.constraint_type)
                    )
                elif constraint.constraint_type is sympy.Le:
                    assert len(constraint) == 1
                    for var in constraint:
                        component = constraint[var]
                        con_models[index] = Constraint(
                            sympy.Ge(- component, 0),
                            model=constraint.model
                        )
        return con_models

    def execute(self, **minimize_options):
        """
        Execute the fit.

        :param minimize_options: keyword arguments to be passed to the specified
            minimizer.
        :return: FitResults instance
        """
        minimizer_ans = self.minimizer.execute(**minimize_options)
        try: # to build covariance matrix
            cov_matrix = minimizer_ans.covariance_matrix
        except AttributeError:
            cov_matrix = self.covariance_matrix(dict(zip(self.model.params, minimizer_ans._popt)))
        else:
            if cov_matrix is None:
                cov_matrix = self.covariance_matrix(dict(zip(self.model.params, minimizer_ans._popt)))
        finally:
            minimizer_ans.covariance_matrix = cov_matrix
        # Overwrite the DummyModel with the current model
        minimizer_ans.model = self.model
        minimizer_ans.gof_qualifiers['r_squared'] = r_squared(self.model, minimizer_ans, self.data)
        return minimizer_ans


def r_squared(model, fit_result, data):
    """
    Calculates the coefficient of determination, R^2, for the fit.

    (Is not defined properly for vector valued functions.)

    :param model: Model instance
    :param fit_result: FitResults instance
    :param data: data with which the fit was performed.
    """
    # First filter out the dependent vars
    y_is = [data[var] for var in model if var in data]
    x_is = [value for var, value in data.items() if var.name in model.__signature__.parameters]
    y_bars = [np.mean(y_i) if y_i is not None else None for y_i in y_is]
    f_is = model(*x_is, **fit_result.params)
    SS_res = np.sum([np.sum((y_i - f_i)**2) for y_i, f_i in zip(y_is, f_is) if y_i is not None])
    SS_tot = np.sum([np.sum((y_i - y_bar)**2) for y_i, y_bar in zip(y_is, y_bars) if y_i is not None])
    return 1 - SS_res/SS_tot

class ODEModel(BaseGradientModel):
    """
    Model build from a system of ODEs. When the model is called, the ODE is
    integrated using the LSODA package.
    """
    def __init__(self, model_dict, initial, *lsoda_args, **lsoda_kwargs):
        """
        :param model_dict: Dictionary specifying ODEs. e.g.
            model_dict = {D(y, x): a * x**2}
        :param initial: ``dict`` of initial conditions for the ODE.
            Must be provided! e.g.
            initial = {y: 1.0, x: 0.0}
        :param lsoda_args: args to pass to the lsoda solver.
            See `scipy's odeint <http://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.odeint.html>`_
            for more info.
        :param lsoda_kwargs: kwargs to pass to the lsoda solver.
        """
        self.initial = initial
        self.lsoda_args = lsoda_args
        self.lsoda_kwargs = lsoda_kwargs

        sort_func = lambda symbol: str(symbol)
        # Mapping from dependent vars to their derivatives
        self.dependent_derivatives = {d: list(d.free_symbols - set(d.variables))[0] for d in model_dict}
        self.dependent_vars = sorted(
            self.dependent_derivatives.values(),
            key=sort_func
        )
        self.independent_vars = sorted(set(d.variables[0] for d in model_dict), key=sort_func)
        self.interdependent_vars = []  # Not yet supported for ODEModels
        if not len(self.independent_vars):
            raise ModelError('ODEModel can only have one independent variable.')

        self.model_dict = OrderedDict(
            sorted(
                model_dict.items(),
                key=lambda i: sort_func(self.dependent_derivatives[i[0]])
            )
        )
        # Extract all the params and vars as a sorted, unique list.
        expressions = model_dict.values()
        model_params = set([])

        # Only the once's that have a Parameter as initial parameter.
        # self.initial_params = {value for var, value in self.initial.items()
        #                        if isinstance(value, Parameter)}

        for expression in expressions:
            vars, params = seperate_symbols(expression)
            model_params.update(params)
            # self.independent_vars.update(vars)
        # Although unique now, params and vars should be sorted alphabetically to prevent ambiguity
        self.params = sorted(model_params, key=sort_func)
        # self.params = sorted(self.model_params | self.initial_params, key=sort_func)
        # self.model_params = sorted(self.model_params, key=sort_func)
        # self.initial_params = sorted(self.initial_params, key=sort_func)

        # Make Variable object corresponding to each sigma var.
        self.sigmas = {var: Variable(name='sigma_{}'.format(var.name)) for var in self.dependent_vars}

        self.__signature__ = self._make_signature()

    def __str__(self):
        """
        Printable representation of this model.

        :return: str
        """
        template = "{}; {}) = {}"
        parts = []
        for var, expr in self.model_dict.items():
            parts.append(template.format(
                    str(var)[:-1],
                    ", ".join(arg.name for arg in self.params),
                    expr
                )
            )
        return "\n".join(parts)

    def __getitem__(self, dependent_var):
        """
        Gives the function defined for the derivative of ``dependent_var``.
        e.g. :math:`y' = f(y, t)`, model[y] -> f(y, t)

        :param dependent_var:
        :return:
        """
        for d, f in self.model_dict.items():
            if dependent_var == self.dependent_derivatives[d]:
                return f

    def __iter__(self):
        """
        :return: iterable over self.model_dict
        """
        return iter(self.dependent_vars)

    def __neg__(self):
        """
        :return: new model with opposite sign. Does not change the model in-place,
            but returns a new copy.
        """
        new_model_dict = self.model_dict.copy()
        for key in new_model_dict:
            new_model_dict[key] *= -1
        return self.__class__(new_model_dict, initial=self.initial)

    @cached_property
    def _ncomponents(self):
        """
        :return: The `numerical_components` for an ODEModel. This differs from
            the traditional `numerical_components`, in that these component can
            also contain dependent variables, not just the independent ones.

            Each of these components does not correspond to e.g. `y(t) = ...`,
            but to `D(y, t) = ...`. The system spanned by these component
            therefore still needs to be integrated.
        """
        return [sympy_to_py(expr, self.independent_vars + self.dependent_vars, self.params)
                for expr in self.values()]

    @cached_property
    def _njacobian(self):
        """
        :return: The `numerical_jacobian` of the components of the ODEModel with
            regards to the dependent variables. This is not to be confused with
            the jacobian of the model as a whole, which is 2D and computed with
            regards to the dependent vars and the fit parameters, and the
            ODEModel still needs to integrated to compute that.

            Instead, this function is used by the ODE integrator, and is not
            meant for human consumption.
        """
        return [
            [sympy_to_py(
                    sympy.diff(expr, var), self.independent_vars + self.dependent_vars, self.params
                ) for var in self.dependent_vars
            ] for _, expr in self.items()
        ]

    def eval_components(self, *args, **kwargs):
        """
        Numerically integrate the system of ODEs.

        :param args: Ordered arguments for the parameters and independent
          variables
        :param kwargs:  Keyword arguments for the parameters and independent
          variables
        :return:
        """
        bound_arguments = self.__signature__.bind(*args, **kwargs)
        t_like = bound_arguments.arguments[self.independent_vars[0].name]

        # System of functions to be integrated
        f = lambda ys, t, *a: [c(t, *(list(ys) + list(a))) for c in self._ncomponents]
        Dfun = lambda ys, t, *a: [[c(t, *(list(ys) + list(a))) for c in row] for row in self._njacobian]

        initial_dependent = [self.initial[var] for var in self.dependent_vars]
        t_initial = self.initial[self.independent_vars[0]] # Assuming there's only one

        # Check if the time-like data includes the initial value, because integration should start there.
        try:
            t_like[0]
        except (TypeError, IndexError): # Python scalar gives TypeError, numpy scalars IndexError
            t_like = np.array([t_like]) # Allow evaluation at one point.

        # The strategy is to split the time axis in a part above and below the
        # initial value, and to integrate those seperately. At the end we rejoin them.
        # np.flip is needed because odeint wants the first point to be t_initial
        # and so t_smaller is a declining series.
        if t_initial in t_like:
            t_bigger = t_like[t_like >= t_initial]
            t_smaller = t_like[t_like <= t_initial][::-1]
        else:
            t_bigger = np.concatenate(
                (np.array([t_initial]), t_like[t_like > t_initial])
            )
            t_smaller = np.concatenate(
                (np.array([t_initial]), t_like[t_like < t_initial][::-1])
            )
        # Properly ordered time axis containing t_initial
        t_total = np.concatenate((t_smaller[::-1][:-1], t_bigger))

        ans_bigger = odeint(
            f,
            initial_dependent,
            t_bigger,
            args=tuple(
                bound_arguments.arguments[param.name] for param in self.params),
            Dfun=Dfun,
            *self.lsoda_args, **self.lsoda_kwargs
        )
        ans_smaller = odeint(
            f,
            initial_dependent,
            t_smaller,
            args=tuple(
                bound_arguments.arguments[param.name] for param in self.params),
            Dfun=Dfun,
            *self.lsoda_args, **self.lsoda_kwargs
        )

        ans = np.concatenate((ans_smaller[1:][::-1], ans_bigger))
        if t_initial in t_like:
            # The user also requested to know the value at t_initial, so keep it.
            return ans.T
        else:
            # The user didn't ask for the value at t_initial, so exclude it.
            # (t_total contains all the t-points used for the integration,
            # and so is t_like with t_initial inserted at the right position).
            return ans[t_total != t_initial].T

    def __call__(self, *args, **kwargs):
        """
        Evaluate the model for a certain value of the independent vars and parameters.
        Signature for this function contains independent vars and parameters, NOT dependent and sigma vars.

        Can be called with both ordered and named parameters. Order is independent vars first, then parameters.
        Alphabetical order within each group.

        :param args: Ordered arguments for the parameters and independent
          variables
        :param kwargs:  Keyword arguments for the parameters and independent
          variables
        :return: A namedtuple of all the dependent vars evaluated at the desired point. Will always return a tuple,
            even for scalar valued functions. This is done for consistency.
        """
        Ans = variabletuple('Ans', self)
        ans = Ans(*self.eval_components(*args, **kwargs))
        return ans


def _partial_diff(var, *params):
    """
    Sympy does not handle repeated partial derivation correctly, e.g.
    D(D(y, a), a) = D(y, a, a) but D(D(y, a), b) = 0.
    Use this function instead to prevent evaluation to zero.
    """
    if isinstance(var, sympy.Derivative):
        return sympy.Derivative(var.expr, *(var.variables + params))
    else:
        return D(var, *params)

def _partial_subs(func, func2vars):
    """
    Partial-bug proof substitution. Works by making the substitutions on
    the expression inside the derivative first, and then rebuilding the
    derivative safely without evaluating it using `_partial_diff`.
    """
    if isinstance(func, sympy.Derivative):
        new_func = func.expr.xreplace(func2vars)
        new_variables = tuple(var.xreplace(func2vars)
                              for var in func.variables)
        return _partial_diff(new_func, *new_variables)
    else:
        return func.xreplace(func2vars)

def jacobian_from_model(model, as_functions=False):
    """
    Build a :class:`~symfit.core.fit.CallableModel` representing the Jacobian of
    ``model``.

    This function make sure the chain rule is correctly applied for
    interdependent variables.

    :param model: Any symbolical model-type.
    :param as_functions: If `True`, the result is returned using
        :class:`sympy.Function` where needed, e.g. {y(x, a): a * x} instead of
        {y: a * x}.
    :return: :class:`~symfit.core.fit.CallableModel` representing the Jacobian
        of ``model``.
    """
    # Inverse dict so we can turn functions back into vars in the end
    functions_as_vars = dict((v, k) for k, v in model.vars_as_functions.items())
    # Create the jacobian components. The `vars` here in the model_dict are
    # always of the type D(y, a), but the righthand-side might still contain
    # functions instead of vars depending on the value of `as_functions`.
    jac = {}
    for func, expr in model.function_dict.items():
        for param in model.params:
            target = D(func, param)
            dfdp = expr.diff(param)
            if as_functions:
                jac[_partial_subs(target, functions_as_vars)] = dfdp
            else:
                # Turn Function objects back into Variables.
                dfdp = dfdp.subs(functions_as_vars, evaluate=False)
                jac[_partial_subs(target, functions_as_vars)] = dfdp
    # Next lines are needed for the Hessian, where the components of model still
    # contain functions instead of vars.
    if as_functions:
        jac.update(model)
    else:
        jac.update({y: expr.subs(functions_as_vars, evaluate=False)
                    for y, expr in model.items()})
    return CallableModel(jac)

def hessian_from_model(model):
    """
    Build a :class:`~symfit.core.fit.CallableModel` representing the Hessian of
    ``model``.

    This function make sure the chain rule is correctly applied for
    interdependent variables.

    :param model: Any symbolical model-type.
    :return: :class:`~symfit.core.fit.CallableModel` representing the Hessian
        of ``model``.
    """
    jac_model = jacobian_from_model(model, as_functions=True)
    return jacobian_from_model(jac_model)

def leastsquares_from_model(model):
    """
    Creates an analytical Least-Squares model for ``model``, which can be used
    to solve analytical least-squares problems.

    .. note: Currently only for use by :class:`LinearLeastSquares`, as no
        analytical sum is included.

    :param model: Any symbolical model-type.
    :return: :class:`~symfit.core.fit.GradientModel` representing the :math:`\chi^2`
        of ``model``.
    """
    chi2_expr = sum(((model[y] - y)**2 / model.sigmas[y]**2)
                    for y in model.dependent_vars)
    chi2 = sympy.Dummy('chi2')
    chi2_dict = {chi2: chi2_expr}
    # Update it with any leftover interdependent variables we might need.
    chi2_dict.update({var: expr for var, expr in model.items()
                     if var not in model.dependent_vars})
    return GradientModel(chi2_dict)