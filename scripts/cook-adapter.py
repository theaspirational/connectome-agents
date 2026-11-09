#!/usr/bin/env python3
"""Run the bundled cook adapter from its immutable repository path."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'roles/connectome_agent/module_utils'))
from connectome_cook import main

if __name__ == '__main__':
    main()
