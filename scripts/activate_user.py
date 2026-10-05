"""Legacy entry point refuses unbounded, unaudited activation."""
def main():
    raise SystemExit("Use scripts/operator_access.py: explicit operator credential, user, organization, expiry, quota and reason required.")

if __name__ == "__main__":
    main()
