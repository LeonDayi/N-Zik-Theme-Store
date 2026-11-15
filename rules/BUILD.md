# Validation

Run the same catalog check as GitHub Actions before publishing:

```powershell
uv run --no-project --with-requirements requirements-validation.txt python .github/scripts/validate_catalog.py
uv run --no-project --with-requirements requirements-validation.txt python -B -m unittest discover -s .github/scripts -p 'test_*.py'
```

Run `git diff --check` and inspect changed manifests, indexes and artwork. For workflow changes, verify the paths and triggers and check the expected GitHub run. For Android integration changes, assemble and test a debug APK in the N-Zik repository.
