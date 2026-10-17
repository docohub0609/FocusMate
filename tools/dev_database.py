"""Initialize the shared local schema before launching independent dev processes."""
from apps.api.problem_storage import ProblemRepository
from apps.api.editor_storage import EditorRepository
from services.oj_worker.editor_runs import RunRepository


def main():
    path = ProblemRepository().path
    EditorRepository(path)
    RunRepository(path)


if __name__ == '__main__':
    main()
