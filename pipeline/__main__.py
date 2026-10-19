import argparse
import sys
import traceback


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline", description="AutoImmune pipeline")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="inject a fault, detect, diagnose, patch, validate, decide, deploy, record")
    r.add_argument("--fault", required=True)
    r.add_argument("--diag-mode", required=True, choices=["rule", "replay", "live"])
    r.add_argument("--patch-mode", required=True, choices=["scripted", "replay", "live"])
    r.add_argument("--candidate", help="deploy this candidate_id instead of the automatic choice (demos)")
    r.add_argument("--deploy-each", action="store_true", help="deploy every AUTO_APPLY candidate in turn")
    r.add_argument("--run-id")
    args = ap.parse_args(argv)
    if args.cmd is None:
        print("AutoImmune pipeline")
        ap.print_help()
        return 0

    from pipeline.orchestrator import StepError, run
    try:
        run(args.fault, args.diag_mode, args.patch_mode, candidate=args.candidate, deploy_each=args.deploy_each,
            run_id=args.run_id)
    except StepError as e:
        traceback.print_exception(e.cause)
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
