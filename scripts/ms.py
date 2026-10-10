"""Print epoch milliseconds for an IST datetime.  Usage: python scripts/ms.py "2026-07-06 09:40:01" """
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
text = sys.argv[1] if len(sys.argv) > 1 else sys.exit('usage: python scripts/ms.py "YYYY-MM-DD HH:MM[:SS]"')
fmt = "%Y-%m-%d %H:%M:%S" if text.count(":") == 2 else "%Y-%m-%d %H:%M"
dt = datetime.strptime(text, fmt).replace(tzinfo=IST)
print(int(dt.timestamp()) * 1000)
