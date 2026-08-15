# Candidate C — "owner-only" credential file permissions are silently not enforced on Windows

*Repo: `glc_v5` only. `S16Code` doesn't store any provider credentials itself (confirmed by grep — no `chmod` calls anywhere in `s16code/`), so this bug and its fix are entirely inside `glc_v5`. Written 2026-08-11.*

## 1. The bug, precisely

`glc_v5` writes two files to disk that hold real secrets and both use the identical, identically-broken pattern:

**`glc/channels/setup.py:56-66`** — `_save()`, which writes `channel_secrets.json` (every channel credential entered via `/channels`: Telegram bot tokens, WhatsApp app secrets, everything):

```python
def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.chmod(temp, 0o600)
    temp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
```

**`glc/config.py:94-108`** — `get_or_create_install_token()`, which writes the sole bootstrap secret that authenticates every WS adapter connection and every `/v1/control/*` request:

```python
def get_or_create_install_token() -> str:
    p = install_token_path()
    if p.exists():
        return p.read_text().strip()
    import secrets
    tok = secrets.token_urlsafe(32)
    p.write_text(tok)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok
```

Both call `os.chmod(path, 0o600)` — "owner read/write only, nobody else" — wrapped in a `try/except OSError: pass`. The `# pragma: no cover - Windows` comment shows the author expected this call to *raise* on Windows. **It doesn't raise. It silently succeeds while doing nothing.** `os.chmod` on Windows can only toggle the read-only DOS attribute bit; it does not touch the file's NTFS ACL (Access Control List), which is what actually governs who can read the file, and which is inherited from the parent folder — typically far more permissive than "owner only." This is the sneakier failure mode: not a crash, a **quiet no-op that looks like success**, exactly the class of bug Session 16 warns is hardest to notice.

**The README's own promise is false on Windows for both files:** *"Settings are stored locally in `~/.glc/channel_secrets.json` with owner-only permissions."*

## 2. The existing evidence this is real

`tests/test_channel_setup.py::test_secret_save_never_round_trips_and_requires_restart` asserts:

```python
assert (tmp_path / "cfg" / "channel_secrets.json").stat().st_mode & 0o077 == 0
```

This fails on Windows today — `st_mode & 0o077` is `63` (`0o077`), i.e. full permissive bits, not `0`. Verify it yourself:

```bash
cd glc_v5
uv run pytest -q tests/test_channel_setup.py::test_secret_save_never_round_trips_and_requires_restart
```

**Independently corroborated**, not just by us: `glc_v5` PR #5 ("Authenticate and bound the generic channel webhook route") ran the full suite and wrote in its own PR body: *"Two failures are pre-existing on a clean checkout of main (`test_secret_save_never_round_trips_and_requires_restart` asserts POSIX file modes and fails on Windows...). Neither is touched by this change."* A second, independent PR author hit the same failure and deliberately left it alone — good confirmation it's real, and confirmation it's still unclaimed as of that PR.

**The gap within the gap:** `install_token`'s identical bug has **zero test coverage**. Checked `tests/conftest.py`'s `install_token` fixture and every test file that references `install_token` — all of them read the token's *content* for use in other tests; none check its *permissions*. So this isn't just an unfixed bug, it's an unmonitored one — if the channel-secrets test didn't happen to exist, nobody would know about either instance.

**Not yet claimed.** Re-verify before starting:
```bash
gh pr list --repo theschoolofai/glc_v5 --state all --json number,title,state,url
```
As of 2026-08-11, none of the open PRs touch `glc/channels/setup.py`'s `_save()` or `glc/config.py`'s `get_or_create_install_token()`.

## 3. A judgment call to make explicitly in the PR

Both files hold real credentials. Weigh this the way `glc_v5` PR #2 weighed a comparable tradeoff (it explicitly reasoned about bounded impact rather than treating every finding as a private-disclosure case): this bug requires **another account already on the same machine** — it is not remotely exploitable, and the PR is describing a missing defense-in-depth measure, not handing over a working exploit against a live service. That reasoning supports a public PR describing the gap plus a regression test plus a fix, the same way PR #2 did for its own finding. State that reasoning explicitly in the PR body, the way PR #2 did, rather than leaving it implicit.

## 4. The fix design

### 4.1 Why `chmod` can't be patched into working — a different mechanism is needed

POSIX permission bits and Windows ACLs are not the same model, and there's no `chmod` flag that bridges them. The dependency-free way to actually restrict a file to the current user on Windows is the `icacls` command-line tool — it ships with every Windows install (confirmed present on this machine at `C:\Windows\System32\icacls.exe`), so this doesn't require adding `pywin32` or any other new dependency, consistent with how the rest of this codebase avoids platform-specific dependencies where it can (see how the sibling `S16Code` tzdata PR chose an unconditional dependency over a platform marker specifically so "a lockfile resolves identically everywhere" — the same value applies here: prefer a solution that doesn't fork the dependency tree by platform).

### 4.2 A single cross-platform helper, used by both call sites

Both `_save()` and `get_or_create_install_token()` currently duplicate the same broken pattern. Fix it once, in one small helper (suggested location: a new `glc/security/file_permissions.py`, or alongside existing file-handling code — whichever fits the codebase's existing module layout better), and call it from both sites:

```python
def restrict_to_owner(path: Path) -> None:
    """Best-effort: make `path` readable/writable only by the current user.

    POSIX honours chmod's owner/group/other bits directly. Windows does not
    — os.chmod there only toggles the read-only DOS attribute and leaves the
    file's NTFS ACL (inherited from its parent folder, typically far more
    permissive) untouched, so a 0o600 chmod call on Windows silently
    protects nothing. icacls is the dependency-free way to actually strip
    that inherited ACL and grant access to the current user alone.
    """
    if platform.system() == "Windows":
        import getpass
        user = getpass.getuser()
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning("could not restrict %s to its owner: %s", path, result.stderr.strip())
    else:
        try:
            os.chmod(path, 0o600)
        except OSError as error:
            logger.warning("could not restrict %s to its owner: %s", path, error)
```

This is a sketch, not a mandate — the implementer should check: correct quoting/escaping for `subprocess.run`'s argument list (already using a list, not `shell=True`, which is the safe form), whether `getpass.getuser()` or `%USERNAME%` is the more reliable source of the current user on this codebase's supported Windows versions, and whether domain-qualified usernames (`DOMAIN\user`) need special handling in the `icacls` grant argument.

**The one behavioral change to insist on regardless of implementation details: stop silently swallowing the failure.** The current `except OSError: pass` is itself part of the bug — it hides the one signal an operator would have that their credential file isn't actually protected. At minimum, log a warning when restriction genuinely fails.

### 4.3 Scope: fix both files

Same root cause, same fix, two call sites (`channel_secrets.json` and `install_token`) — fix both in one PR rather than leaving the second one to be independently rediscovered later. It also closes the "zero test coverage" gap noted in §2.

## 5. Test strategy

**POSIX:** the existing assertion in `test_secret_save_never_round_trips_and_requires_restart` (`st_mode & 0o077 == 0`) stays valid and should keep passing unchanged.

**Windows:** `st_mode` doesn't reflect real ACL state even after an `icacls`-based fix (it's a fairly approximated value on Windows), so the assertion needs a different mechanism. Two options:

- **Parse `icacls path` output** (no modification flags) and assert that only the expected principal(s) appear — the current user, plus `NT AUTHORITY\SYSTEM` and `BUILTIN\Administrators` which are standard/expected — and that broad groups like `Everyone`, `BUILTIN\Users`, or `Authenticated Users` do **not** appear. Workable and dependency-free, but flag as a known fragility: `icacls`'s text output format can vary by locale, so this needs a reasonably tolerant parser, not an exact string match.
- Alternative: skip parsing and instead do a **behavioral** check — attempt to open the file as a different (non-owner) principal and assert it's denied. Harder to set up portably in a test environment; probably not worth the complexity for this PR.

Recommend the `icacls`-parsing approach. Add it as a **new** test for `install_token`'s permissions too, since none currently exists (§2) — don't just fix the existing `channel_secrets.json` test and leave `install_token` uncovered again.

## 6. Concrete touch points

- New (or appropriately placed existing) module: `restrict_to_owner(path: Path) -> None` per §4.2.
- `glc/channels/setup.py:_save()` — replace both `os.chmod(temp, 0o600)` and the `try/os.chmod(path, 0o600)/except OSError: pass` block with calls to the new helper.
- `glc/config.py:get_or_create_install_token()` — same replacement.
- `tests/test_channel_setup.py` — keep the existing POSIX assertion; add a Windows-specific assertion per §5.
- New test file or addition covering `install_token_path()`'s permissions specifically (currently absent entirely).

## 7. Explicitly out of scope for this PR

- Don't also fix Candidate D (the budget concurrency race) in the same PR — different bug, different file, keep the diff reviewable.
- Don't add `pywin32` or any other new dependency — `icacls` via `subprocess` is sufficient and matches the codebase's existing preference for avoiding platform-specific dependencies.
- Don't try to handle exotic platforms beyond POSIX and Windows (e.g. this doesn't need to reason about ACL semantics on any other OS) — `platform.system() == "Windows"` vs. everything else (existing `os.chmod` path) is a sufficient split for this codebase's supported targets.
