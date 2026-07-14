from apk_migrate_tui.diff import compute_diff, filter_entries
from apk_migrate_tui.models import AppInfo, DiffStatus


def make_app(pkg, version_code, version_name="1.0"):
    return AppInfo(package=pkg, version_code=version_code, version_name=version_name)


def test_identical_version_is_identical():
    src = {"com.a": make_app("com.a", 5)}
    tgt = {"com.a": make_app("com.a", 5)}
    entries = compute_diff(src, tgt)
    assert len(entries) == 1
    assert entries[0].status == DiffStatus.IDENTICAL


def test_different_version_flagged():
    src = {"com.a": make_app("com.a", 6)}
    tgt = {"com.a": make_app("com.a", 5)}
    entries = compute_diff(src, tgt)
    assert entries[0].status == DiffStatus.VERSION_DIFF


def test_source_only():
    src = {"com.a": make_app("com.a", 6)}
    tgt = {}
    entries = compute_diff(src, tgt)
    assert entries[0].status == DiffStatus.SOURCE_ONLY


def test_target_only():
    src = {}
    tgt = {"com.a": make_app("com.a", 6)}
    entries = compute_diff(src, tgt)
    assert entries[0].status == DiffStatus.TARGET_ONLY


def test_unknown_version_code_treated_as_diff_not_identical():
    # Safety-critical: if we couldn't read a version (dumpsys failure), never silently
    # treat it as "identical" and skip it - that could cause a stale app to be missed.
    src = {"com.a": AppInfo(package="com.a", version_code=None)}
    tgt = {"com.a": AppInfo(package="com.a", version_code=None)}
    entries = compute_diff(src, tgt)
    assert entries[0].status == DiffStatus.VERSION_DIFF


def test_filter_hides_identical_when_requested():
    src = {"com.a": make_app("com.a", 5), "com.b": make_app("com.b", 6)}
    tgt = {"com.a": make_app("com.a", 5), "com.b": make_app("com.b", 5)}
    entries = compute_diff(src, tgt)
    visible = filter_entries(entries, hide_identical=True, show_target_only=False)
    assert len(visible) == 1
    assert visible[0].package == "com.b"


def test_filter_search_matches_package_name():
    src = {"com.example.foo": make_app("com.example.foo", 1), "com.example.bar": make_app("com.example.bar", 1)}
    entries = compute_diff(src, {})
    visible = filter_entries(entries, hide_identical=False, show_target_only=False, search="foo")
    assert len(visible) == 1
    assert visible[0].package == "com.example.foo"


def test_filter_excludes_target_only_by_default():
    entries = compute_diff({}, {"com.a": make_app("com.a", 1)})
    visible = filter_entries(entries, hide_identical=False, show_target_only=False)
    assert visible == []
    visible2 = filter_entries(entries, hide_identical=False, show_target_only=True)
    assert len(visible2) == 1
