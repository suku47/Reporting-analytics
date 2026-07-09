"""Generate license keys for customers."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.core.traf_security import generate_customer_license, get_machine_id

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate customer license key")
    parser.add_argument('--customer', required=True, help='Customer name')
    parser.add_argument('--days', type=int, default=365, help='Validity in days')
    parser.add_argument('--machine-lock', action='store_true')
    args = parser.parse_args()

    mid = get_machine_id() if args.machine_lock else None
    lic = generate_customer_license(args.customer, args.days, machine_id=mid)
    print(f"License Key:  {lic['license_key']}")
    print(f"Customer:     {lic['customer_name']}")
    print(f"Expires:      {lic['expiry_date'] or 'Never'}")
    if mid:
        print(f"Machine ID:   {mid}")
