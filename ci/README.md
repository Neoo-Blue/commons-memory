The GitHub Actions workflow lives here temporarily because the token used to publish lacked the `workflow` scope.
Enable CI with:

    mkdir -p .github/workflows && git mv ci/github-actions-ci.yml .github/workflows/ci.yml && git commit -m "Enable CI" && git push
