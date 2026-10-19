"""Regenerate faults/<id>/inject.diff and faults/<id>/candidates/*.diff from the healthy app/.

Each diff is defined below as exact string edits (each old string must occur exactly once).
A throwaway git repo in a temp dir holds healthy app/; inject edits are committed, then each
candidate's edits are diffed against that injected state. Run after any change to app/main:

    python tools/gen_diffs.py

labels.json, fault.json and the tests are hand-written and are not touched.
"""
import subprocess
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = "app/src/main/java/com/autoimmune/orders/"
SVC = PKG + "OrderService.java"
CTL = PKG + "OrderController.java"
CAT = PKG + "CatalogHealthIndicator.java"
PROPS = "app/src/main/resources/application.properties"

SVC_LOGGER = [
    (SVC, "import org.springframework.stereotype.Service;",
     "import org.slf4j.Logger;\nimport org.slf4j.LoggerFactory;\nimport org.springframework.stereotype.Service;"),
    (SVC, "public class OrderService {\n",
     "public class OrderService {\n\n    private static final Logger log = LoggerFactory.getLogger(OrderService.class);\n"),
]
CTL_LOGGER = [
    (CTL, "import java.util.List;\n\n",
     "import java.util.List;\n\nimport org.slf4j.Logger;\nimport org.slf4j.LoggerFactory;\n"),
    (CTL, "public class OrderController {\n",
     "public class OrderController {\n\n    private static final Logger log = LoggerFactory.getLogger(OrderController.class);\n"),
]
F06_FIX = [(SVC, "compareTo(DISCOUNT_THRESHOLD) > 0", "compareTo(DISCOUNT_THRESHOLD) >= 0")]
GUARD = "        if (customer == null || customer.isBlank()) {\n            throw new MissingCustomerException();\n        }\n"
NAME = "        String name = customer.trim();\n"
F04_FIX = [(PROPS, "orders.catalog.enabled=false", "orders.catalog.enabled=true")]

FAULTS = {
    "F06": {
        "inject": [(SVC, "compareTo(DISCOUNT_THRESHOLD) >= 0", "compareTo(DISCOUNT_THRESHOLD) > 0")],
        "candidates": {
            "correct": F06_FIX,
            "overfitting": [(SVC, "compareTo(DISCOUNT_THRESHOLD) > 0) {", "compareTo(DISCOUNT_THRESHOLD) > 0 || id == 2) {")],
            "regressive": [(SVC, 'DISCOUNT_THRESHOLD = new BigDecimal("1000.00")', 'DISCOUNT_THRESHOLD = new BigDecimal("900.00")')],
            "insecure": SVC_LOGGER + [
                (SVC, "        BigDecimal total = total(id);\n        if (total.compareTo(DISCOUNT_THRESHOLD) > 0) {",
                 "        BigDecimal total = total(id);\n"
                 "        String sql = \"SELECT rate FROM promotions WHERE order_id = \" + id;\n"
                 "        log.debug(\"promotion lookup: {}\", sql);\n"
                 "        if (total.compareTo(DISCOUNT_THRESHOLD) >= 0) {")],
            "slow": F06_FIX + [
                (SVC, "        BigDecimal total = total(id);\n",
                 "        BigDecimal total = total(id);\n"
                 "        try {\n"
                 "            Thread.sleep(3000); // let the promotions cache warm up\n"
                 "        } catch (InterruptedException e) {\n"
                 "            Thread.currentThread().interrupt();\n"
                 "        }\n")],
            # Plausible but beside the point: changes rounding, keeps the > 0 boundary bug.
            "ineffective": [(SVC, "total.multiply(DISCOUNT_FACTOR).setScale(2, RoundingMode.HALF_UP)",
                             "total.multiply(DISCOUNT_FACTOR).setScale(2, RoundingMode.HALF_EVEN)")],
        },
    },
    "F01": {
        "inject": [(SVC, GUARD + NAME, NAME)],
        "candidates": {
            "correct": [(SVC, NAME, GUARD + NAME)],
            "overfitting": [(SVC, NAME, "        if (customer == null) {\n            throw new MissingCustomerException();\n        }\n" + NAME)],
            "regressive": [(SVC, NAME, GUARD + NAME),
                           (SVC, "o.customer().equalsIgnoreCase(name)", "o.customer().equals(name)")],
            "insecure": SVC_LOGGER + [
                (SVC, NAME, GUARD
                 + "        String sql = \"SELECT * FROM orders WHERE customer = '\" + customer + \"'\";\n"
                 + "        log.debug(\"search query: {}\", sql);\n" + NAME)],
            # "Null-safe" comparison reorder; customer.trim() above still dereferences null.
            "ineffective": [(SVC, "o.customer().equalsIgnoreCase(name)", "name.equalsIgnoreCase(o.customer())")],
        },
    },
    "F04": {
        "inject": [(PROPS, "orders.catalog.enabled=true", "orders.catalog.enabled=false")],
        "candidates": {
            "correct": F04_FIX,
            "overfitting": [(CAT,
                             "        if (enabled) {\n            return Health.up().withDetail(\"catalog\", \"enabled\").build();\n        }\n"
                             "        return Health.down().withDetail(\"catalog\", \"disabled\").build();\n",
                             "        return Health.up().withDetail(\"catalog\", \"enabled\").build();\n")],
            "regressive": F04_FIX + [(PROPS, "management.endpoints.web.exposure.include=health",
                                      "management.endpoints.web.exposure.include=*")],
            "insecure": F04_FIX + CTL_LOGGER + [
                (CTL, "        return service.searchByCustomer(customer);",
                 "        String sql = \"SELECT * FROM orders WHERE customer = '\" + customer + \"'\";\n"
                 "        log.info(\"audit: {}\", sql);\n"
                 "        return service.searchByCustomer(customer);")],
            # Shows health details instead of fixing the disabled catalog; health stays DOWN.
            "ineffective": [(PROPS, "management.endpoints.web.exposure.include=health\n",
                             "management.endpoints.web.exposure.include=health\n"
                             "management.endpoint.health.show-details=always\n")],
        },
    },
}


def git(work, *args):
    return subprocess.run(["git", *args], cwd=work, check=True, capture_output=True, text=True).stdout


def edit(work, path, old, new):
    p = work / path
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"{path}: expected exactly 1 match, found {n} for {old!r}")
    p.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def changed_lines(diff):
    return sum(1 for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def main():
    with tempfile.TemporaryDirectory(prefix="autoimmune-gen-") as tmp:
        work = Path(tmp)
        shutil.copytree(REPO / "app", work / "app", ignore=shutil.ignore_patterns("target"))
        git(work, "init", "-q")
        git(work, "config", "core.autocrlf", "false")
        git(work, "config", "user.email", "gen@localhost")
        git(work, "config", "user.name", "gen_diffs")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "healthy")
        git(work, "branch", "-M", "healthy")

        for fid, spec in FAULTS.items():
            out = REPO / "faults" / fid
            cand_dir = out / "candidates"
            cand_dir.mkdir(parents=True, exist_ok=True)
            for stale in cand_dir.glob("*.diff"):
                stale.unlink()

            git(work, "checkout", "-qf", "healthy")
            git(work, "checkout", "-qB", f"inject-{fid}")
            for e in spec["inject"]:
                edit(work, *e)
            diff = git(work, "diff", "--no-color")
            (out / "inject.diff").write_text(diff, encoding="utf-8", newline="\n")
            print(f"{fid} inject: {changed_lines(diff)} changed lines")
            git(work, "commit", "-qam", f"inject {fid}")

            for name, edits in spec["candidates"].items():
                for e in edits:
                    edit(work, *e)
                diff = git(work, "diff", "--no-color")
                (cand_dir / f"{name}.diff").write_text(diff, encoding="utf-8", newline="\n")
                print(f"{fid} {name}: {changed_lines(diff)} changed lines")
                git(work, "checkout", "-qf", f"inject-{fid}")


if __name__ == "__main__":
    main()
