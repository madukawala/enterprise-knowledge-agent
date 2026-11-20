"""Shared building blocks used by every backend service: settings, logging and errors.

This package sits at the bottom of the dependency graph. It must not import
from `api`, `security` or `ui` (checked by import-linter).
"""
