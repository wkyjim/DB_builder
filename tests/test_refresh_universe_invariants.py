"""Invariant tests for production refresh universe.

These tests fail if the production universe drifts again:
- REQUIRED_SYMBOLS must equal APPROVED_MACRO_SYMBOLS
- SOURCE_DEFINITION_BY_SYMBOL must cover all 68
- SYMBOL_CLOSE_MAP must cover all 68
- INVESTINY_ASSETS must exactly equal the approved Investing subset
- USnYT=X must use Investing source only
- USnYT_CMT must use Treasury source only
"""

import ast
import importlib.util
import os
import re
import sys

# Project paths
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
SESSION_CALENDAR_PATH = os.path.join(PROJECT_ROOT, 'src', 'db_builder', 'session_calendar.py')
MACRO_DATA_FETCH_PATH = os.path.join(PROJECT_ROOT, 'scripts', 'macro_data_fetch.py')
SRC_PATH = os.path.join(PROJECT_ROOT, 'src')


def _load_by_path(path, module_name):
    """Import a module from an explicit path.

    Preferred over AST parsing: static analysis of set-union / dict-comprehension
    definitions is brittle and silently under-reports. Importing the real module
    asserts against the values production actually uses.
    """
    if SRC_PATH not in sys.path:
        sys.path.insert(0, SRC_PATH)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        # Must be registered before exec_module: @dataclass resolves
        # sys.modules[cls.__module__].__dict__ and raises AttributeError
        # ('NoneType' has no '__dict__') when the module is absent.
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(module_name, None)
        return None


def _load_session_calendar():
    """Load session_calendar.py. It is stdlib-only, so this is always safe."""
    return _load_by_path(SESSION_CALENDAR_PATH, "_session_calendar_under_test")


def _load_macro_data_fetch():
    """Load macro_data_fetch.py (needs db_builder.config). May return None."""
    return _load_by_path(MACRO_DATA_FETCH_PATH, "macro_data_fetch_under_test")


def _read_file(path):
    with open(path, 'r') as f:
        return f.read()


def _extract_approved_symbols():
    """Extract APPROVED_MACRO_SYMBOLS, preferring a real import.

    Falls back to AST when macro_data_fetch.py cannot be imported (it needs
    db_builder.config and the environment file).
    """
    module = _load_macro_data_fetch()
    if module is not None:
        approved = getattr(module, "APPROVED_MACRO_SYMBOLS", None)
        if approved is not None:
            return set(approved)
    content = _read_file(MACRO_DATA_FETCH_PATH)
    tree = ast.parse(content)
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'APPROVED_MACRO_SYMBOLS':
                    # Extract all string constants from the frozenset
                    if isinstance(node.value, ast.Call):
                        for arg in node.value.args:
                            if isinstance(arg, ast.Set):
                                return {elt.value for elt in arg.elts if isinstance(elt, ast.Constant)}
    raise RuntimeError("APPROVED_MACRO_SYMBOLS not found")


def _extract_required_symbols_assignment():
    """Check if REQUIRED_SYMBOLS = APPROVED_MACRO_SYMBOLS."""
    content = _read_file(MACRO_DATA_FETCH_PATH)
    return 'REQUIRED_SYMBOLS = APPROVED_MACRO_SYMBOLS' in content


def _extract_source_definitions():
    """Extract SOURCE_DEFINITION_BY_SYMBOL, preferring a real import.

    The AST fallback is retained only for environments where the module cannot
    be imported. It under-reports set-union / dict-comprehension entries, so a
    fallback-only result must be treated as unreliable.
    """
    module = _load_session_calendar()
    if module is not None:
        mapping = getattr(module, "SOURCE_DEFINITION_BY_SYMBOL", None)
        if mapping:
            return dict(mapping)
    content = _read_file(SESSION_CALENDAR_PATH)
    
    # We need to resolve the set names used in dict comprehensions
    # First, extract all module-level set definitions
    tree = ast.parse(content)
    sets = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                        if node.value.func.id == 'frozenset':
                            for arg in node.value.args:
                                if isinstance(arg, ast.Set):
                                    sets[target.id] = {elt.value for elt in arg.elts if isinstance(elt, ast.Constant)}
                                elif isinstance(arg, ast.Constant):
                                    sets[target.id] = {arg.value}
                    elif isinstance(node.value, ast.Constant):
                        sets[target.id] = node.value.value
    
    # Now extract SOURCE_DEFINITION_BY_SYMBOL
    source_defs = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == 'SOURCE_DEFINITION_BY_SYMBOL' and node.value:
                if isinstance(node.value, ast.Dict):
                    for key, val in zip(node.value.keys, node.value.values):
                        if isinstance(key, ast.Constant) and isinstance(val, ast.Name):
                            source_defs[key.value] = val.id
                        elif isinstance(key, ast.Constant) and isinstance(val, ast.Constant):
                            source_defs[key.value] = val.value
                    # Handle dict comprehensions (DictComp)
                    for key, val in zip(node.value.keys, node.value.values):
                        if isinstance(key, ast.DictComp) or (isinstance(node.value, ast.Dict) and not isinstance(key, ast.Constant)):
                            pass
                    # Check for DictComp in values
                    for val in node.value.values:
                        if isinstance(val, ast.DictComp):
                            # Extract the comprehension
                            if isinstance(val.value, ast.Name):
                                value_name = val.value.id
                            elif isinstance(val.value, ast.Constant):
                                value_name = val.value.value
                            else:
                                continue
                            
                            # Handle set unions
                            if isinstance(val.generators[0].iter, ast.BinOp):
                                left = val.generators[0].iter.left
                                right = val.generators[0].iter.right
                                symbols = set()
                                if isinstance(left, ast.Name) and left.id in sets:
                                    symbols |= sets[left.id]
                                if isinstance(right, ast.Name) and right.id in sets:
                                    symbols |= sets[right.id]
                                if isinstance(right, ast.Call) and isinstance(right.func, ast.Name):
                                    if right.func.id == 'set' and isinstance(right.args[0], ast.Name):
                                        if right.args[0].id in sets:
                                            symbols |= sets[right.args[0].id]
                                for sym in symbols:
                                    source_defs[sym] = value_name
                            # Handle tuple literals
                            elif isinstance(val.generators[0].iter, ast.Tuple):
                                for elt in val.generators[0].iter.elts:
                                    if isinstance(elt, ast.Constant):
                                        source_defs[elt.value] = value_name
                            # Handle single set
                            elif isinstance(val.generators[0].iter, ast.Name):
                                if val.generators[0].iter.id in sets:
                                    for sym in sets[val.generators[0].iter.id]:
                                        source_defs[sym] = value_name
    
    return source_defs


def _extract_close_map():
    """Extract SYMBOL_CLOSE_MAP, preferring a real import.

    NOTE: SYMBOL_CLOSE_MAP is defined in scripts/macro_data_fetch.py, not in
    session_calendar.py (which only references it in a comment). Reading the
    wrong file made this helper return an empty set, so
    test_symbol_close_map_covers_all_68 could never pass.
    """
    module = _load_macro_data_fetch()
    if module is not None:
        mapping = getattr(module, "SYMBOL_CLOSE_MAP", None)
        if mapping:
            return set(mapping)
    content = _read_file(MACRO_DATA_FETCH_PATH)
    tree = ast.parse(content)
    
    # First extract all module-level set definitions
    sets = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                        if node.value.func.id == 'frozenset':
                            for arg in node.value.args:
                                if isinstance(arg, ast.Set):
                                    sets[target.id] = {elt.value for elt in arg.elts if isinstance(elt, ast.Constant)}
    
    close_map = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == 'SYMBOL_CLOSE_MAP' and node.value:
                if isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        if isinstance(key, ast.Constant):
                            close_map.add(key.value)
                    for val in node.value.values:
                        if isinstance(val, ast.DictComp):
                            if isinstance(val.generators[0].iter, ast.Name):
                                if val.generators[0].iter.id in sets:
                                    close_map |= sets[val.generators[0].iter.id]
                            elif isinstance(val.generators[0].iter, ast.BinOp):
                                left = val.generators[0].iter.left
                                right = val.generators[0].iter.right
                                if isinstance(left, ast.Name) and left.id in sets:
                                    close_map |= sets[left.id]
                                if isinstance(right, ast.Name) and right.id in sets:
                                    close_map |= sets[right.id]
    
    return close_map


def _extract_investiny_assets():
    """Extract INVESTINY_ASSETS, preferring a real import."""
    module = _load_macro_data_fetch()
    if module is not None:
        assets = getattr(module, "INVESTINY_ASSETS", None)
        if assets is not None:
            return set(assets)
    content = _read_file(MACRO_DATA_FETCH_PATH)
    tree = ast.parse(content)
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'INVESTINY_ASSETS':
                    if isinstance(node.value, ast.Dict):
                        return {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    raise RuntimeError("INVESTINY_ASSETS not found")


def _extract_unresolved():
    """Extract SOURCE_DEFINITION_UNRESOLVED, preferring a real import."""
    module = _load_session_calendar()
    if module is not None:
        unresolved = getattr(module, "SOURCE_DEFINITION_UNRESOLVED", None)
        if unresolved is not None:
            return set(unresolved)
    content = _read_file(SESSION_CALENDAR_PATH)
    tree = ast.parse(content)
    
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == 'SOURCE_DEFINITION_UNRESOLVED' and node.value:
                if isinstance(node.value, ast.Call):
                    for arg in node.value.args:
                        if isinstance(arg, ast.Set):
                            return {elt.value for elt in arg.elts if isinstance(elt, ast.Constant)}
                        return set()
                elif isinstance(node.value, ast.Constant):
                    return set()
    return set()


# ---- Tests ----

def test_approved_universe_count():
    """APPROVED_MACRO_SYMBOLS must contain exactly 69 symbols."""
    approved = _extract_approved_symbols()
    assert len(approved) == 69, f"Expected 69 approved symbols, got {len(approved)}"


def test_required_symbols_equals_approved():
    """REQUIRED_SYMBOLS must equal APPROVED_MACRO_SYMBOLS."""
    content = _read_file(MACRO_DATA_FETCH_PATH)
    assert 'REQUIRED_SYMBOLS = APPROVED_MACRO_SYMBOLS' in content, \
        "REQUIRED_SYMBOLS must be derived from APPROVED_MACRO_SYMBOLS"
    
    # Old 20-symbol frozenset should be gone
    match = re.search(r'REQUIRED_SYMBOLS\s*=\s*frozenset\(\s*\{([^}]+)\}\s*\)', content, re.DOTALL)
    assert match is None, "REQUIRED_SYMBOLS should not be a manually maintained frozenset anymore"


def test_source_definition_covers_all_68():
    """SOURCE_DEFINITION_BY_SYMBOL must cover all 68 approved symbols."""
    approved = _extract_approved_symbols()
    source_defs = _extract_source_definitions()
    
    missing = approved - set(source_defs.keys())
    assert len(missing) == 0, f"Missing source definitions for: {sorted(missing)}"


def test_symbol_close_map_covers_all_68():
    """SYMBOL_CLOSE_MAP must cover all 68 approved symbols."""
    approved = _extract_approved_symbols()
    close_map = _extract_close_map()
    
    missing = approved - close_map
    assert len(missing) == 0, f"Missing close map entries for: {sorted(missing)}"


def test_investiny_assets_equals_investing_subset():
    """INVESTINY_ASSETS must exactly equal the approved Investing subset."""
    approved = _extract_approved_symbols()
    investiny_assets = _extract_investiny_assets()
    source_defs = _extract_source_definitions()
    
    # All INVESTINY_ASSETS must be in approved universe
    extra = investiny_assets - approved
    assert len(extra) == 0, f"INVESTINY_ASSETS contains non-approved symbols: {sorted(extra)}"
    
    # All approved Investing symbols must be in INVESTINY_ASSETS
    investing_in_approved = set()
    for sym in approved:
        sd = source_defs.get(sym, '')
        if 'INVESTING' in sd:
            investing_in_approved.add(sym)
    
    missing = investing_in_approved - investiny_assets
    extra = investiny_assets - investing_in_approved
    assert len(missing) == 0, f"Approved Investing symbols missing from INVESTINY_ASSETS: {sorted(missing)}"
    assert len(extra) == 0, f"INVESTINY_ASSETS contains non-Investing symbols: {sorted(extra)}"


def test_usnyt_investing_source_only():
    """All USnYT=X symbols must use Investing source, not Treasury."""
    source_defs = _extract_source_definitions()
    
    usnyt = ['US2YT=X', 'US3YT=X', 'US5YT=X', 'US7YT=X', 'US10YT=X', 'US20YT=X', 'US30YT=X']
    for sym in usnyt:
        sd = source_defs.get(sym, '')
        assert 'INVESTING' in sd, f"{sym} must use Investing source, got: {sd}"
        assert 'TREASURY' not in sd, f"{sym} must NOT use Treasury source"


def test_usnyt_cmt_treasury_source_only():
    """All USnYT_CMT symbols must use Treasury.gov source, not Investing."""
    source_defs = _extract_source_definitions()
    
    cmt = ['US2YT_CMT', 'US3YT_CMT', 'US5YT_CMT', 'US7YT_CMT', 'US10YT_CMT', 'US20YT_CMT', 'US30YT_CMT']
    for sym in cmt:
        sd = source_defs.get(sym, '')
        assert 'TREASURY' in sd, f"{sym} must use Treasury source, got: {sd}"
        assert 'INVESTING' not in sd, f"{sym} must NOT use Investing source"


def test_no_approved_symbol_excluded_from_refresh():
    """No approved symbol must be silently excluded from normal refresh routing."""
    content = _read_file(MACRO_DATA_FETCH_PATH)
    assert 'REQUIRED_SYMBOLS = APPROVED_MACRO_SYMBOLS' in content, \
        "REQUIRED_SYMBOLS must be derived from APPROVED_MACRO_SYMBOLS"
    
    unresolved = _extract_unresolved()
    assert len(unresolved) == 0, \
        f"SOURCE_DEFINITION_UNRESOLVED should be empty, got: {unresolved}"


def test_special_futures_have_source_definitions():
    """CIHc1, HK50, KOR200c1 must have confirmed Investing source definitions."""
    source_defs = _extract_source_definitions()
    
    # Map each symbol to the SOURCE_DEF_* constant it must use. The constants
    # are plain strings that omit the SOURCE_DEF_ prefix, so resolve them from
    # the module rather than hardcoding either form.
    expected_const = {
        'CIHc1': 'SOURCE_DEF_INVESTING_SSE50_FUTURES_DAILY',
        'HK50': 'SOURCE_DEF_INVESTING_HK40_FUTURES_DAILY',
        'KOR200c1': 'SOURCE_DEF_INVESTING_KOSPI200_FUTURES_DAILY',
    }

    module = _load_session_calendar()
    for sym, const_name in expected_const.items():
        actual = source_defs.get(sym)
        if module is not None:
            expected_def = getattr(module, const_name)
        else:
            expected_def = const_name[len('SOURCE_DEF_'):]
        assert actual == expected_def, f"{sym}: expected {expected_def}, got {actual}"
