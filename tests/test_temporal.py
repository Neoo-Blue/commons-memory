import runpy


def test_relative_dates_resolve_against_the_memory_date():
    runpy.run_module("commons_memory.temporal", run_name="__main__")  # the module's own assertion table
