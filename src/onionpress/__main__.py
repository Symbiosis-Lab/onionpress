"""Allow running as: python -m onionpress"""

import sys
from .cli import main

sys.exit(main())
