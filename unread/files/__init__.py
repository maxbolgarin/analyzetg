"""Local-file analysis: PDF / DOCX / text / code / audio / video / image / stdin.

Mirrors the YouTube and website analyzers — `cmd_analyze_file` extracts
text (or transcribes / describes) the file, builds synthetic messages
in the same shape as `youtube/commands.py:_build_synthetic_messages`,
and runs them through `analyzer.pipeline.run_analysis`.

Extractors live in :mod:`unread.files.extractors`. Cache + pipeline
live in :mod:`unread.files.commands`.
"""
