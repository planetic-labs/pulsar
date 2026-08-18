# Contributing to Pulsar

First off, thank you for considering contributing to Pulsar! It's people like you that make Pulsar such a great tool for the community.

## Code of Conduct

By participating in this project, you agree to abide by the same standards of professional and respectful behavior expected in any major open-source community.

## How Can I Contribute?

### Reporting Bugs

- **Check if the bug has already been reported** by searching on GitHub under [Issues](https://github.com/planetic-labs/pulsar/issues).
- If you can't find an open issue addressing the problem, [open a new one](https://github.com/planetic-labs/pulsar/issues/new). Be sure to include a **clear title and description**, as much relevant information as possible, and a **code sample** or an **executable test case** demonstrating the expected behavior that is not occurring.

### Suggesting Enhancements

- Open a GitHub Issue with the "enhancement" label.
- Provide a clear and concise description of the proposed feature.
- Explain why this enhancement would be useful to Pulsar users.

### Pull Requests

1.  **Fork the repository** and create your branch from `main`.
2.  If you've added code that should be tested, **add tests**.
3.  Ensure the test suite passes.
4.  Make sure your code follows the project's coding style (see below).
5.  Issue that Pull Request!

## Development Setup

Pulsar uses [uv](https://github.com/astral-sh/uv) for dependency management and Python environment control.

### Prerequisites

- Python 3.14 or higher.
- `uv` installed (`pip install uv`).
- Docker and Docker Compose (for integration testing).

### Local Setup

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/planetic-labs/pulsar.git
    cd pulsar
    ```
2.  **Install dependencies**:
    ```bash
    uv sync
    ```
3.  **Install pre-commit hooks**:
    ```bash
    uv run pre-commit install
    ```
    This will ensure that `ruff` and `ty` checks run before every commit.

## Coding Standards

### Linting and Formatting

We use [Ruff](https://github.com/astral-sh/ruff) for both linting and formatting. 
Before submitting a PR, please run:
```bash
uv run ruff check . --fix
uv run ruff format .
```

### Type Checking

We use `ty` for type analysis. Ensure your changes pass the type check:
```bash
uv run ty check
```

### Testing

Always verify your changes by running the test suite:
```bash
uv run pytest
```

## Branching Strategy

- `main`: The stable branch. All production-ready code lives here.
- Feature branches: Create a new branch for each feature or bugfix (e.g., `feat/add-new-search-provider` or `fix/handle-empty-transcripts`).

## Pull Request Process

1.  Ensure any installations or build dependencies are removed before the end of the layer when doing a build.
2.  Update the `README.md` or `docs/` with details of changes to the interface, this includes new environment variables, exposed ports, or location of database files.
3.  The PR will be merged once it has been reviewed and approved by at least one maintainer.

Thank you for your contribution!
