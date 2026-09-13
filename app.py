import sys

from core.discovery import discover


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 3:
        print("Product Data Verifier")
        return

    brand, model = sys.argv[1:3]
    article = sys.argv[3] if len(sys.argv) > 3 else None
    candidates = discover(brand, model, article)
    if not candidates:
        print("No candidates found.")
        return

    print(f"Candidates for {brand} {model}:")
    for index, candidate in enumerate(candidates[:10], 1):
        print(
            f"{index:>2}. [{candidate['score']:>3}] {candidate['source_type']:<12} "
            f"{candidate['authority_status']:<8} {candidate['model_match']:<8} "
            f"{candidate['domain']}\n"
            f"    {candidate['title'] or '(no title)'}\n"
            f"    {candidate['url']}\n"
            f"    authority evidence: {candidate['authority_evidence_url'] or '-'}\n"
            f"    product evidence: {candidate['product_match_evidence'] or '-'}"
        )


if __name__ == "__main__":
    main()
