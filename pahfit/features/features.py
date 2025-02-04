"""
pahfit.features

Manage PAHFIT features, their parameters, and parameter attributes.
The PAHFIT model contains a flexible combination of features, each of
which have associated parameters and parameter attributes.  These
parameters are specified by a combination of a named science pack, and
instrument-specific information such as wavelength-dependent line
resolution.

The main class, Features, inherits from astropy.table.Table.  All the
grouping, sorting, selection, and indexing operations from astropy
tables are therefore also available for pahfit.features.Features.

  Usage:

  TBD
"""

import os
import numpy as np
from astropy.table import vstack, Table, TableAttribute
from astropy.io.misc.yaml import yaml
import astropy.units as u
from pkg_resources import resource_filename
from pahfit.errors import PAHFITFeatureError
from pahfit.features.features_format import BoundedMaskedColumn, BoundedParTableFormatter


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = set()
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise PAHFITFeatureError(f"Duplicate {key!r} key found in YAML.")
            mapping.add(key)
        return super().construct_mapping(node, deep)


def value_bounds(val, bounds):
    """Compute bounds for a bounded value.

    Arguments:
    ----------
      val: The value to bound.

      bounds: Either None for no relevant bounds (i.e. fixed), or a
        two element iterable specifying (min, max) bounds.  Each of
        min and max can be a numerical value, None (for infinite
        bounds, either negative or positive, as appropriate), or a
        string ending in:

          #: an absolute offset from the value
          %: a percentage offset from the value

        Offsets are necessarily negative for min bound, positive for
        max bounds.

    Examples:
    ---------

      A bound of ('-1.5%', '0%') would indicate a minimum bound
        1.5% below the value, and a max bound at the value itself.

      A bound of ('-0.1#', None) would indicate a minimum bound 0.1 below
        the value, and infinite maximum bound.

    Returns:
    -------

      The value, if unbounded, or a 3 element tuple (value, min, max).
        Any missing bound is replaced with the numpy `masked' value.

    Raises:
    -------

      ValueError: if bounds are specified and the value does not fall
        between them.

    """
    if val is None:
        val = np.ma.masked
    if not bounds:
        return (val,) + 2 * (np.ma.masked,)  # Fixed
    ret = [val]
    for i, b in enumerate(bounds):
        if isinstance(b, str):
            if b.endswith('%'):
                b = val * (1. + float(b[:-1]) / 100.)
            elif b.endswith('#'):
                b = val + float(b[:-1])
            else:
                raise PAHFITFeatureError(f"Incorrectly formatted bound {b}")
        elif b is None:
            b = np.inf if i else -np.inf
        ret.append(b)

    if (val < ret[1] or val > ret[2]):
        raise PAHFITFeatureError(f"Value <{ret[0]}> is not between bounds: {ret[1:]}")
    return tuple(ret)


class Features(Table):
    """A class for holding PAHFIT features and their associated
    parameter information.  Note that each parameter has an associated
    `kind', and that each kind has an associated set of allowable
    parameters (see _kind_params, below).
    """

    TableFormatter = BoundedParTableFormatter
    MaskedColumn = BoundedMaskedColumn

    param_covar = TableAttribute(default=[])
    _kind_params = {'starlight': {'temperature', 'tau'},
                    'dust_continuum': {'temperature', 'tau'},
                    'line': {'wavelength', 'power'},  # 'fwhm', Instrument Pack detail!
                    'dust_feature': {'wavelength', 'fwhm', 'power'},
                    'attenuation': {'model', 'tau', 'geometry'},
                    'absorption': {'wavelength', 'fwhm', 'tau', 'geometry'}}

    _units = {'temperature': u.K, 'wavelength': u.um, 'fwhm': u.um}
    _group_attrs = set(('bounds', 'features', 'kind'))  # group-level attributes
    _param_attrs = set(('value', 'bounds'))  # Each parameter can have these attributes
    _no_bounds = set(('name', 'group', 'geometry', 'model'))  # String attributes (no bounds)

    @classmethod
    def read(cls, file, *args, **kwargs):
        """Read a table from file.

        If reading a YAML file, read it in as a science pack and
        return the new table. Otherwise, use astropy's normal Table
        reader.
        """
        if file.endswith(".yaml") or file.endswith(".yml"):
            return cls._read_scipack(file)
        else:
            return super().read(file, *args, **kwargs)

    @classmethod
    def _read_scipack(cls, file):
        """Read a science pack specification from YAML file.

        Arguments:
        ----------

          file: the name of the file, either a full valid path, or
            named file in the PAHFIT science_packs directory.!

        Returns:
        --------

          table: A filled pahfit.features.Features table.
        """

        feat_tables = dict()

        if not os.path.isfile(file):
            pack_path = resource_filename("pahfit", "packs/science")
            file = os.path.join(pack_path, file)
        try:
            with open(file) as fd:
                scipack = yaml.load(fd, Loader=UniqueKeyLoader)
        except IOError as e:
            raise PAHFITFeatureError("Error reading science pack file\n"
                                     f"\t{file}\n\t{repr(e)}")
        for (name, elem) in scipack.items():
            try:
                keys = elem.keys()
            except AttributeError:
                raise PAHFITFeatureError("Invalid science pack"
                                         f" format at {name}\n\t{file}")

            try:
                kind = elem.pop('kind')
            except KeyError:
                raise PAHFITFeatureError(f"No kind found for {name}\n\t{file}")

            try:
                valid_params = cls._kind_params[kind]
            except KeyError:
                raise PAHFITFeatureError(f"Unknown kind {kind} for {name}\n\t{file}")
            unknown_params = [x for x in keys
                              if not (x in valid_params or x in cls._group_attrs)]
            if unknown_params:
                raise PAHFITFeatureError(f"Unknown {kind} parameters:"
                                         f" {', '.join(unknown_params)}\n\t{file}")

            hasFeatures = 'features' in elem
            hasLists = any(k not in cls._group_attrs
                           and (isinstance(v, (tuple, list))
                                or (isinstance(v, dict)
                                    and cls._param_attrs.isdisjoint(v.keys())))
                           for (k, v) in elem.items())
            if hasFeatures and hasLists:
                raise PAHFITFeatureError("A single group cannot contain both 'features'"
                                         f" and parameter list(s): {name}\n\t{file}")
            isGroup = (hasFeatures or hasLists)
            bounds = None
            if isGroup:  # A named group of features
                if 'bounds' in elem:
                    if not isinstance(elem['bounds'], dict):
                        for p in cls._no_bounds:
                            if p in elem:
                                raise PAHFITFeatureError(f"Parameter {p} cannot have "
                                                         f"bounds: {name}\n\t{file}")
                        if sum(p in elem for p in valid_params) > 1:
                            raise PAHFITFeatureError("Groups with simple bounds "
                                                     "can only specify a single "
                                                     f"parameter: {name}\n\t{file}")
                        if hasFeatures:
                            raise PAHFITFeatureError("Groups with simple bounds "
                                                     "cannot specify "
                                                     f"'features': {name}\n\t{file}")
                    bounds = elem.pop('bounds')
                if hasFeatures:  # our group uses a features dict
                    for n, v in elem['features'].items():
                        if bounds and 'bounds' not in v:  # inherit bounds
                            v['bounds'] = bounds
                        cls._add_feature(kind, feat_tables, n, group=name, **v)
                elif hasLists:  # a "shortcut" feature group, using lists
                    llen = []
                    for k, v in elem.items():
                        if k in cls._group_attrs:
                            continue
                        if not isinstance(v, (tuple, list, dict)):
                            raise PAHFITFeatureError(f"All non-group parameters in {name} "
                                                     f"must be lists or dicts:\n\t{file}")
                        llen.append(len(v))

                    if not all(x == llen[0] for x in llen):
                        raise PAHFITFeatureError(f"All parameter lists in group {name} "
                                                 f"must be the same length:\n\t{file}")
                    ngroup = llen[0]
                    feat_names = None
                    for k, v in elem.items():
                        if isinstance(elem[k], dict):
                            if not feat_names:  # First names win
                                feat_names = list(elem[k].keys())
                            elem[k] = list(elem[k].values())  # turn back into a value list
                    if not feat_names:  # no names: construct one for each group feature
                        feat_names = [f"{name}{x:02}" for x in range(ngroup)]
                    for i in range(ngroup):  # Iterate over list(s) adding feature
                        v = {k: elem[k][i] for k in valid_params if k in elem}
                        cls._add_feature(kind, feat_tables, feat_names[i],
                                         group=name, bounds=bounds, **v)
                else:
                    raise PAHFITFeatureError(f"Group {name} needs either 'features' or"
                                             f"parameter list(s):\n\t{file}")
            else:  # Just one standalone feature
                cls._add_feature(kind, feat_tables, name, **elem)
        return cls._construct_table(feat_tables)

    @classmethod
    def _add_feature(cls, kind: str, t: dict, name: str, *,
                     bounds=None, group='_none_', **pars):
        """Adds an individual feature to the passed dictionary t."""
        if kind not in t:
            t[kind] = {}  # group by kind
        if name not in t[kind]:
            t[kind][name] = {}
        t[kind][name]['group'] = group
        t[kind][name]['kind'] = kind
        for (param, val) in pars.items():
            if param not in cls._kind_params[kind]:
                continue
            if isinstance(val, dict):  # A param attribute dictionary
                unknown_attrs = [x for x in val.keys() if x not in cls._param_attrs]
                if unknown_attrs:
                    raise PAHFITFeatureError("Unknown parameter attributes for"
                                             f" {name} ({kind}, {group}): "
                                             f"{', '.join(unknown_attrs)}")
                if 'value' not in val:
                    raise PAHFITFeatureError("Missing 'value' attribute for "
                                             f"{name} ({kind}, {group})")
                value = val['value']
                if 'bounds' in val:  # individual param bounds
                    if param in cls._no_bounds:
                        raise PAHFITFeatureError("Parameter {param} cannot have bounds: "
                                                 f"{name} ({kind}, {group})")
                    bounds = val['bounds']
            else:
                value = val  # a bare value
            if isinstance(bounds, dict):
                b = bounds.get(param)
                if b and param in cls._no_bounds:
                    raise PAHFITFeatureError("Parameter {param} cannot have bounds: "
                                             f"{name} ({kind}, {group})")
            else:  # Simple bounds
                b = bounds
            try:
                t[kind][name][param] = (value if param in cls._no_bounds
                                        else value_bounds(value, b))
            except ValueError as e:
                raise PAHFITFeatureError("Error initializing value and bounds for"
                                         f" {name} ({kind}, {group}):\n\t{e}")

    @classmethod
    def _construct_table(cls, inp: dict):
        """Construct a masked table with units from input dictionary
        INP.  INP is a dictionary with feature names as the key, and a
        dictionary of feature parameters as value.  Each value in the
        feature parameter dictionary is either a value or tuple of 3
        values for bounds.
        """
        tables = []
        for (kind, features) in inp.items():
            kind_params = cls._kind_params[kind]  # All params for this kind
            rows = []
            for (name, params) in features.items():
                for missing in kind_params - params.keys():
                    if missing in cls._no_bounds:
                        params[missing] = 0.0
                    else:
                        params[missing] = value_bounds(0.0, bounds=(0.0, None))
                rows.append(dict(name=name, **params))
            table_columns = rows[0].keys()
            t = cls(rows, names=table_columns)
            for p in cls._kind_params[kind]:
                if p not in cls._no_bounds:
                    t[p].info.format = "0.4g"  # Nice format (customized by Formatter)
            tables.append(t)
        tables = vstack(tables)
        for cn, col in tables.columns.items():
            if cn in cls._units:
                col.unit = cls._units[cn]
        tables.add_index('name')
        return tables
