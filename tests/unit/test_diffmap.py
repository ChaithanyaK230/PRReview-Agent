from core.github.diffmap import DiffMap

DIFF = "\n".join([
    # Modified file: 1 line removed, 2 added
    "diff --git a/app/billing.py b/app/billing.py",
    "index 1111111..2222222 100644",
    "--- a/app/billing.py",
    "+++ b/app/billing.py",
    "@@ -1,4 +1,5 @@",
    " def total(items):",
    "-    return sum(items)",
    "+    subtotal = sum(items)",
    "+    return subtotal",
    " ",
    " # end",
    # New file
    "diff --git a/app/new.py b/app/new.py",
    "new file mode 100644",
    "index 0000000..3333333",
    "--- /dev/null",
    "+++ b/app/new.py",
    "@@ -0,0 +1,2 @@",
    "+x = 1",
    "+y = 2",
    # Renamed and modified
    "diff --git a/old/util.py b/new/util.py",
    "similarity index 90%",
    "rename from old/util.py",
    "rename to new/util.py",
    "index 4444444..5555555 100644",
    "--- a/old/util.py",
    "+++ b/new/util.py",
    "@@ -1,2 +1,2 @@",
    " def f():",
    "-    return 1",
    "+    return 2",
    # Binary file: must be ignored
    "diff --git a/logo.png b/logo.png",
    "index 6666666..7777777 100644",
    "Binary files a/logo.png and b/logo.png differ",
]) + "\n"


def make() -> DiffMap:
    return DiffMap.from_diff(DIFF)


def test_modified_file_sides():
    d = make()
    assert d.right["app/billing.py"] == {1, 2, 3, 4, 5}
    assert d.left["app/billing.py"] == {1, 2, 3, 4}
    assert d.added["app/billing.py"] == {2, 3}


def test_new_file_all_lines_added():
    d = make()
    assert d.added["app/new.py"] == {1, 2}
    assert d.left["app/new.py"] == set()


def test_rename_uses_new_path_and_records_old():
    d = make()
    assert "new/util.py" in d.right
    assert d.renamed == {"new/util.py": "old/util.py"}


def test_binary_file_skipped():
    assert "logo.png" not in make().right


def test_commentable():
    d = make()
    assert d.commentable("app/billing.py", 2, "RIGHT")
    assert d.commentable("app/billing.py", 2, "LEFT")       # the removed line
    assert not d.commentable("app/billing.py", 99, "RIGHT")
    assert not d.commentable("missing.py", 1, "RIGHT")


def test_nearest():
    d = make()
    assert d.nearest("app/billing.py", 3) == 3     # already valid
    assert d.nearest("app/billing.py", 7) == 5     # 2 lines away
    assert d.nearest("app/billing.py", 10) is None  # more than 3 lines away