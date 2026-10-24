from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil


class PreDeployValidationException(Exception):
    """Raised when a pre-deployment validation hook fails."""
    pass


class _HTMLAssetRefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, _ = self.getpos()
        for attr_name, attr_val in attrs:
            if attr_name in ("src", "href") and attr_val:
                self.refs.append((line, f"<{tag} {attr_name}>", attr_val.strip()))


_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b([^>]*?)>", re.IGNORECASE | re.DOTALL)

_IGNORED_PREFIXES = (
    "http://",
    "https://",
    "//",
    "mailto:",
    "tel:",
    "javascript:",
    "data:",
    "#",
)

_FORBIDDEN_SECRET_PATTERNS = {
    ".env",
    "id_rsa",
    "id_ed25519",
}
_FORBIDDEN_SECRET_SUFFIXES = {
    ".pem",
    ".key",
    ".pfx",
    ".p12",
}
_OS_JUNK_FILES = {
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
}

CLOUDFLARE_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MiB
CLOUDFLARE_MAX_FILE_COUNT = 20_000


def _iter_project_files(root: Path):
    """Yields all files under `root`, excluding `.git` and `.idea` directories."""
    for path in root.rglob("*"):
        if any(part in {".git", ".idea", "__pycache__", "node_modules"} for part in path.parts):
            continue
        if path.is_file():
            yield path


def _has_exact_case_path(root: Path, target: Path) -> tuple[bool, str | None]:
    """
    Verifies that `target` exists and that every path component from `root`
    to `target` matches the exact casing on disk (critical for Windows -> Cloudflare).
    Returns (is_valid, actual_Disk_path_if_case_mismatch).
    """
    if not target.exists():
        return False, None

    try:
        rel_parts = target.relative_to(root).parts
    except ValueError:
        return False, None

    curr = root
    actual_parts: list[str] = []
    mismatch = False

    for part in rel_parts:
        entries = {p.name.lower(): p.name for p in curr.iterdir()}
        actual_name = entries.get(part.lower())
        if actual_name is None:
            return False, None
        if actual_name != part:
            mismatch = True
        actual_parts.append(actual_name)
        curr = curr / actual_name

    if mismatch:
        return False, "/".join(actual_parts)
    return True, None


# ---------------------------------------------------------------------------
# Hook 1: Case-Sensitive Link & Asset Validator
# ---------------------------------------------------------------------------
def validate_case_sensitive_links(project_home: str | Path) -> int:
    """
    Parses all .html and .css files in `project_home` and verifies every local
    src/href/url() target exists on disk with an exact case match.
    Returns the total number of local references validated.
    """
    root = Path(project_home).resolve()
    errors: list[str] = []
    checked_count = 0

    for file_path in _iter_project_files(root):
        ext = file_path.suffix.lower()
        rel_file = file_path.relative_to(root).as_posix()
        refs: list[tuple[int, str, str]] = []

        if ext == ".html":
            parser = _HTMLAssetRefParser()
            parser.feed(file_path.read_text(encoding="utf-8", errors="replace"))
            refs = parser.refs
        elif ext == ".css":
            text = file_path.read_text(encoding="utf-8", errors="replace")
            for idx, line in enumerate(text.splitlines(), start=1):
                for match in _CSS_URL_RE.finditer(line):
                    url_val = match.group(2).strip()
                    if url_val:
                        refs.append((idx, "url()", url_val))
        else:
            continue

        for line_no, source_kind, raw_ref in refs:
            if raw_ref.lower().startswith(_IGNORED_PREFIXES):
                continue

            # Strip query string and fragment
            clean_ref = raw_ref.split("#", 1)[0].split("?", 1)[0].strip()
            if not clean_ref:
                continue

            checked_count += 1
            if clean_ref.startswith("/"):
                candidate = (root / clean_ref.lstrip("/")).resolve()
            else:
                candidate = (file_path.parent / clean_ref).resolve()

            is_valid, actual_cased = _has_exact_case_path(root, candidate)
            if not is_valid:
                if actual_cased:
                    errors.append(
                        f"  - {rel_file}:{line_no} ({source_kind} '{raw_ref}'): "
                        f"Case mismatch! On disk it is '{actual_cased}'"
                    )
                else:
                    errors.append(
                        f"  - {rel_file}:{line_no} ({source_kind} '{raw_ref}'): "
                        f"Target file does not exist"
                    )

    if errors:
        error_report = "\n".join(errors)
        raise PreDeployValidationException(
            f"Broken or case-mismatched local asset links detected:\n{error_report}"
        )

    return checked_count


# ---------------------------------------------------------------------------
# Hook 2: Automatic `loading="lazy"` & `decoding="async"` Injection on <img>
# ---------------------------------------------------------------------------
def inject_lazy_loading_in_html(project_home: str | Path) -> tuple[int, int]:
    """
    Ensures all <img> tags across .html files have loading="lazy" and decoding="async".
    Returns (files_modified, img_tags_updated).
    """
    root = Path(project_home).resolve()
    files_modified = 0
    tags_updated = 0

    for file_path in _iter_project_files(root):
        if file_path.suffix.lower() != ".html":
            continue

        original_text = file_path.read_text(encoding="utf-8", errors="replace")
        file_tags_updated = 0

        def _replace_img(match: re.Match) -> str:
            nonlocal file_tags_updated
            attrs = match.group(1)
            additions: list[str] = []

            if not re.search(r"\bloading\s*=", attrs, re.IGNORECASE):
                additions.append('loading="lazy"')
            if not re.search(r"\bdecoding\s*=", attrs, re.IGNORECASE):
                additions.append('decoding="async"')

            if not additions:
                return match.group(0)

            file_tags_updated += 1
            extra = " " + " ".join(additions)
            if attrs.rstrip().endswith("/"):
                stripped = attrs.rstrip()[:-1].rstrip()
                return f"<img{stripped}{extra} />"
            return f"<img{attrs}{extra}>"

        updated_text = _IMG_TAG_RE.sub(_replace_img, original_text)
        if file_tags_updated > 0 and updated_text != original_text:
            file_path.write_text(updated_text, encoding="utf-8")
            files_modified += 1
            tags_updated += file_tags_updated

    return files_modified, tags_updated


# ---------------------------------------------------------------------------
# Hook 3: Unused Font & Unminified Vendor File Pruning
# ---------------------------------------------------------------------------
def prune_unused_assets(project_home: str | Path) -> tuple[int, int]:
    """
    Removes unreferenced legacy font formats (.ttf, variable_ttf, font demo files)
    and unminified vendor JS/CSS files when a corresponding .min.js/.min.css exists.
    Returns (files_removed, bytes_saved).
    """
    root = Path(project_home).resolve()
    files_removed = 0
    bytes_saved = 0

    # 1. Remove unused .ttf font directories and font specimen/demo files
    fonts_dir = root / "fonts"
    if fonts_dir.exists():
        for folder_name in ("ttf", "variable_ttf"):
            for ttf_dir in fonts_dir.rglob(folder_name):
                if ttf_dir.is_dir():
                    for f in ttf_dir.rglob("*"):
                        if f.is_file():
                            bytes_saved += f.stat().st_size
                            files_removed += 1
                    shutil.rmtree(ttf_dir)

        for unused_file_name in ("specimen.html", "fira_code.css", "README.txt"):
            for unused_file in fonts_dir.rglob(unused_file_name):
                if unused_file.is_file():
                    bytes_saved += unused_file.stat().st_size
                    files_removed += 1
                    unused_file.unlink()

    # 2. Remove unminified vendor .js/.css when a sibling .min.js/.min.css exists
    vendor_dir = root / "vendor"
    if vendor_dir.exists():
        for file_path in list(vendor_dir.rglob("*")):
            if not file_path.is_file():
                continue
            ext = file_path.suffix.lower()
            if ext in {".js", ".css"} and not file_path.name.lower().endswith(f".min{ext}"):
                minified_sibling = file_path.with_name(f"{file_path.stem}.min{ext}")
                if minified_sibling.exists():
                    bytes_saved += file_path.stat().st_size
                    files_removed += 1
                    file_path.unlink()

    return files_removed, bytes_saved


# ---------------------------------------------------------------------------
# Hook 5: Cloudflare Asset Limit & Secret Guardrail
# ---------------------------------------------------------------------------
def validate_asset_limits_and_secrets(project_home: str | Path) -> int:
    """
    Removes OS junk files (.DS_Store, Thumbs.db) and verifies:
      - No secret/key files (.env, .pem, .key, etc.) exist in the deploy tree.
      - No file exceeds Cloudflare Workers Static Assets' 25 MiB limit.
      - Total file count does not exceed Cloudflare's 20,000 file limit.
    Returns total number of valid deployable files.
    """
    root = Path(project_home).resolve()
    violations: list[str] = []
    file_count = 0

    for file_path in list(_iter_project_files(root)):
        lower_name = file_path.name.lower()

        # Automatically clean up OS metadata junk files
        if lower_name in _OS_JUNK_FILES:
            file_path.unlink()
            continue

        file_count += 1
        rel_path = file_path.relative_to(root).as_posix()

        # Check for secret/credential files
        if (
            lower_name in _FORBIDDEN_SECRET_PATTERNS
            or lower_name.startswith(".env.")
            or file_path.suffix.lower() in _FORBIDDEN_SECRET_SUFFIXES
        ):
            violations.append(f"  - Forbidden secret/credential file in project tree: {rel_path}")

        # Check Cloudflare 25 MiB per-file limit
        size = file_path.stat().st_size
        if size > CLOUDFLARE_MAX_FILE_SIZE_BYTES:
            size_mb = size / (1024 * 1024)
            violations.append(
                f"  - File exceeds Cloudflare 25 MiB limit ({size_mb:.2f} MiB): {rel_path}"
            )

    if file_count > CLOUDFLARE_MAX_FILE_COUNT:
        violations.append(
            f"  - Total file count ({file_count}) exceeds Cloudflare limit ({CLOUDFLARE_MAX_FILE_COUNT})"
        )

    if violations:
        report = "\n".join(violations)
        raise PreDeployValidationException(
            f"Cloudflare asset limit or security check failed:\n{report}"
        )

    return file_count


# ---------------------------------------------------------------------------
# Hook 6: Auto-Generate sitemap.xml
# ---------------------------------------------------------------------------
def generate_sitemap(
    project_home: str | Path,
    base_url: str = "https://natebrowndev.com",
) -> bool:
    """
    Scans public .html files in `project_home` and generates/updates `sitemap.xml`
    and ensures `robots.txt` references it.
    Returns True if `sitemap.xml` or `robots.txt` was created or updated.
    """
    root = Path(project_home).resolve()
    base_url = base_url.rstrip("/")
    excluded_names = {"error.html", "404.html", "specimen.html"}

    html_files: list[Path] = []
    for file_path in _iter_project_files(root):
        if file_path.suffix.lower() != ".html":
            continue
        if file_path.name.lower() in excluded_names:
            continue
        if "fonts" in file_path.relative_to(root).parts:
            continue
        html_files.append(file_path)

    html_files.sort(key=lambda p: p.relative_to(root).as_posix())

    url_entries: list[str] = []
    for html_file in html_files:
        rel_posix = html_file.relative_to(root).as_posix()
        loc = f"{base_url}/" if rel_posix == "index.html" else f"{base_url}/{rel_posix}"
        mtime = datetime.fromtimestamp(html_file.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        priority = "1.0" if rel_posix == "index.html" else ("0.8" if "/" not in rel_posix else "0.6")
        url_entries.append(
            f"  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{mtime}</lastmod>\n"
            f"    <priority>{priority}</priority>\n"
            f"  </url>"
        )

    sitemap_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(url_entries)
        + "\n</urlset>\n"
    )

    changed = False
    sitemap_path = root / "sitemap.xml"

    # Only rewrite sitemap.xml if URLs or content changed (ignoring date-only churn if unchanged)
    existing_xml = sitemap_path.read_text(encoding="utf-8") if sitemap_path.exists() else ""
    strip_lastmod = lambda s: re.sub(r"<lastmod>.*?</lastmod>", "", s)
    if strip_lastmod(existing_xml) != strip_lastmod(sitemap_content):
        sitemap_path.write_text(sitemap_content, encoding="utf-8")
        changed = True

    robots_path = root / "robots.txt"
    sitemap_directive = f"Sitemap: {base_url}/sitemap.xml"
    if robots_path.exists():
        robots_text = robots_path.read_text(encoding="utf-8")
        if "Sitemap:" not in robots_text:
            updated_robots = robots_text.rstrip() + f"\n{sitemap_directive}\n"
            robots_path.write_text(updated_robots, encoding="utf-8")
            changed = True

    return changed
