# Publishing the repository

This is a source-preparation guide. Nothing in the repository creates a GitHub
repository, pushes commits, publishes packages, or creates releases automatically.

## First push

1. Open a terminal in the project directory.
2. Review `git status --short`. Local environments, credentials, firmware,
   caches, and build outputs should not be listed as new files.
3. Create an **empty** repository on GitHub. Leave automatic README/license
   initialization disabled because these files already exist locally.
4. Stage and review the source files, then create the initial commit:

   ```sh
   git add .
   git diff --cached --stat
   git commit -m "Initial public source release"
   ```

5. Replace `YOUR_USERNAME` with the repository owner's actual GitHub login:

   ```sh
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/pixel-eub.git
   git push -u origin main
   ```

   If `origin` is already configured, inspect `git remote -v` first instead
   of adding it again. Do not overwrite an existing remote without reviewing it.

6. Inspect the Actions tab. The workflow runs linting, unit tests, Linux PTY
   tests, a build, and an installed-wheel smoke check. Local Linux success
   does not establish that the other matrix jobs have passed on GitHub.

Keep the description accurate: **Tensor Pixel ROM Recovery tooling with
request-driven serial uploads**. Describe the forced panic continuation as
experimental. The compatibility table
describes available image sources, not a guarantee that every Pixel can boot.

## Files and release artifacts

Commit the source tree, documentation, and `.github` configuration. Do not commit
`.venv`, `dist`, caches, firmware, real-device dumps, credentials, or private logs.
The `.gitignore` and `MANIFEST.in` exclusions cover these common cases.

`python -m build` creates a Python wheel and source distribution under `dist/`.
They may be attached to a release after review. Neither contains the recovery
firmware. A source-preparation ZIP, if provided separately, must be extracted
before committing its contents; uploading the ZIP as one repository file does
not install the project or enable its workflow.

Enable private vulnerability reporting in repository settings if available.
Before distributing any firmware separately, review that material's own terms;
the project's MIT license does not cover downloaded firmware.
