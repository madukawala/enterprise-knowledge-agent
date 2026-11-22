# Contributing

Thank you for taking the time to contribute. This page explains how changes reach the `develop` and `main` branches, and what to check before you open a pull request.

## Branches

| Branch | Purpose | Who can change it |
|---|---|---|
| `main` | The released, stable code | Only the repository owner, by merging a pull request from `develop` |
| `develop` | Where finished work is collected before a release | Only the repository owner, by merging reviewed pull requests |
| Your own branch | Where you make your change | You |

Nobody can push directly to `main` or `develop`, and nobody except the owner can merge into them. These rules are enforced by GitHub, not just by convention. Every change goes through a pull request into `develop`.

## How to contribute a change

### 1. Get the code

If you are not a collaborator on this repository, fork it on GitHub first and clone your fork. If you are a collaborator, clone this repository directly.

```bash
git clone https://github.com/<your-account>/enterprise-knowledge-agent.git
cd enterprise-knowledge-agent
```

When you work from a fork, add this repository as `upstream` so you can keep `develop` up to date:

```bash
git remote add upstream https://github.com/madukawala/enterprise-knowledge-agent.git
```

### 2. Start from the latest develop

Always branch from `develop`, never from `main`.

```bash
git checkout develop
git pull upstream develop      # or: git pull origin develop, if you cloned this repository directly
```

### 3. Create your branch

Name the branch after the kind of change and what it does:

| Prefix | Use it for | Example |
|---|---|---|
| `feature/` | New functionality | `feature/hybrid-retrieval` |
| `fix/` | Bug fixes | `fix/mask-typed-objects-in-traces` |
| `other/` | Documentation, tooling, clean-up and anything else | `other/fine-tuning-the-platform` |

```bash
git checkout -b feature/short-description
```

### 4. Make the change and check it locally

Set up the project once with `make install`. This installs the dependencies and the pre-commit hooks. See [Development](README.md#development) in the README for the full setup.

Before you push, run the same checks CI runs:

```bash
make check
```

This runs the lint, type checks, import boundaries, tests with the 80% coverage gate, and the security checks. If you changed the Docker setup, also run `make compose-check` and `make scan-image`.

Write commit messages as one plain sentence that says what the change does, for example "Mask typed objects from the agent state in LangSmith traces".

### 5. Push your branch

```bash
git push -u origin feature/short-description
```

### 6. Open a pull request into develop

On GitHub, open a pull request from your branch into **`develop`**. Do not target `main`.

In the description, explain what changed and why, how you tested it, and anything the reviewer should look at closely.

## What happens after you open the pull request

1. **CI runs.** Five checks must pass: lint, types and import boundaries; tests and coverage; security checks; secret scan; and the Compose check with image scans. If you opened the pull request from a fork, CI starts only after the owner approves the run.
2. **The owner reviews it.** The [CODEOWNERS](.github/CODEOWNERS) file requests the owner's review automatically. All review comments must be resolved before merging.
3. **Updating your pull request.** Push new commits to the same branch and the pull request updates itself. A new push clears any earlier approval, so the latest version is always the one that was reviewed. If `develop` has moved on since you branched, update your branch from `develop` and push again.
4. **The owner merges it** into `develop`. You cannot merge it yourself, even after it is approved. Your branch is deleted after the merge.

## Releases: develop into main

Only the owner releases. The owner opens a pull request from `develop` into `main`, CI runs again, and the owner merges it.

A pull request into `main` from any other branch, or from a fork, fails the "PRs to main must come from develop" check and cannot be merged. This check is defined in [.github/workflows/branch-policy.yml](.github/workflows/branch-policy.yml).

## Summary

```
your branch  -->  pull request into develop  -->  owner reviews and merges
develop      -->  pull request into main     -->  owner merges (owner only)
```
