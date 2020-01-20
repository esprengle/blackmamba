#!python3

import io
import re
from enum import Enum
import editor
import console
from blackmamba.ide.annotation import Annotation, Style
from itertools import groupby
from blackmamba.config import get_config_value
import blackmamba.ide.tab as tab
import os
import blackmamba.log as log


def _hud_alert_delay():
    return get_config_value('analyzer.hud_alert_delay', 1.0)


def _ignore_codes():
    return get_config_value('analyzer.ignore_codes', ['W391', 'W293'])


def _max_line_length():
    return get_config_value('analyzer.max_line_length', 79)


def _remove_whitespaces():
    return get_config_value('analyzer.remove_whitespaces', True)


_REMOVE_TRAILING_WHITESPACES_REGEX = re.compile(r'[ \t]+$', re.MULTILINE)
_REMOVE_TRAILING_LINES_REGEX = re.compile(r'\s+\Z', re.MULTILINE)

#
# Common for pep8 & pyflakes
#


class _Source(Enum):
    pep8 = 'PEP8'
    pyflakes = 'pyflakes'
    flake8 = 'flake8'


class _AnalyzerAnnotation(Annotation):
    def __init__(self, line, text, source, style):
        super().__init__(line, text, style)
        self.source = source

    def __lt__(self, other):
        if self.source is _Source.pep8 and other.source is _Source.pyflakes:
            return True

        if self.source is _Source.flake8 and other.source is not _Source.flake8:
            return True

        if self.style is Style.warning and other.style is Style.error:
            return True

        return False


#
# pep8
#

def _pep8_annotations(text, ignore=None, max_line_length=None):
    import pep8

    class _Pep8AnnotationReport(pep8.BaseReport):
        def __init__(self, options):
            super().__init__(options)
            self.annotations = []

        def error(self, line_number, offset, text, check):
            # If super doesn't return code, this one is ignored
            if not super().error(line_number, offset, text, check):
                return

            annotation = _AnalyzerAnnotation(self.line_offset + line_number, text, _Source.pep8, Style.warning)
            self.annotations.append(annotation)

    # pep8 requires you to include \n at the end of lines
    lines = text.splitlines(True)

    style_guide = pep8.StyleGuide(reporter=_Pep8AnnotationReport, )
    options = style_guide.options

    if ignore:
        options.ignore = tuple(ignore)
    else:
        options.ignore = tuple()

    if max_line_length:
        options.max_line_length = max_line_length

    checker = pep8.Checker(None, lines, options, None)
    checker.check_all()

    return checker.report.annotations


#
# pyflakes
#

_LINE_COL_MESSAGE_REGEX = re.compile(r'^(\d+):(\d+): (.*)$')
_LINE_MESSAGE_REGEX = re.compile(r'^(\d+): (.*)$')


def _get_annotations(path, stream, style):
    path_len = len(path)

    annotations = []
    for line in stream.getvalue().splitlines():
        if not line.startswith(path):
            continue

        line = line[(path_len + 1):]  # Strip 'filename:'
        match = _LINE_COL_MESSAGE_REGEX.fullmatch(line)

        if not match:
            match = _LINE_MESSAGE_REGEX.fullmatch(line)

        if not match:
            continue

        line = int(match.group(1))

        if match.lastindex == 3:
            annotation = _AnalyzerAnnotation(
                line, 'Col {}: {}'.format(match.group(2), match.group(3)),
                _Source.pyflakes, style
            )
        else:
            annotation = _AnalyzerAnnotation(
                line, match.group(2),
                _Source.pyflakes, style
            )

        annotations.append(annotation)

    return annotations


def _pyflakes_annotations(path, text):
    import pyflakes.api as pyflakes

    warning_stream = io.StringIO()
    error_stream = io.StringIO()
    reporter = pyflakes.modReporter.Reporter(warning_stream, error_stream)

    pyflakes.check(text, path, reporter)

    warnings = _get_annotations(path, warning_stream, Style.warning)
    errors = _get_annotations(path, error_stream, Style.error)

    return warnings + errors

#
# flake8
#


def _parse_flake8_output(path, output_path):
    path_len = len(path)

    annotations = []

    with open(output_path, 'r') as output:
        report = output.read()
        for line in report.splitlines():
            if not line.startswith(path):
                continue

            line = line[(path_len + 1):]  # Strip 'filename:'
            match = _LINE_COL_MESSAGE_REGEX.fullmatch(line)

            if not match:
                match = _LINE_MESSAGE_REGEX.fullmatch(line)

            if not match:
                continue

            line = int(match.group(1))

            def get_style(message):
                return Style.warning if message.startswith('W') else Style.error

            if match.lastindex == 3:
                annotation = _AnalyzerAnnotation(
                    line, 'Col {}: {}'.format(match.group(2), match.group(3)),
                    _Source.flake8, get_style(match.group(3))
                )
            else:
                annotation = _AnalyzerAnnotation(
                    line, match.group(2),
                    _Source.flake8, get_style(match.group(2))
                )

            annotations.append(annotation)

    return annotations


def _flake8_annotations(path, options):
    import os

    _tmp = os.environ.get('TMPDIR', os.environ.get('TMP'))
    _output_file = os.path.join(_tmp, 'blackmamba.flake8.txt')

    annotations = []

    for o in options:
        try:
            from flake8.main import application

            if os.path.exists(_output_file):
                os.remove(_output_file)

            o = list(o)
            o.insert(0, path)
            o.extend([
                '-j', '0',  # Disable subprocess
                '--output-file={}'.format(_output_file)
            ])
            app = application.Application()
            app.run(o)
            del app

            annotations.extend(_parse_flake8_output(path, _output_file))
        except Exception as e:
            log.error('flake8 failed: {}'.format(str(e)))

    if os.path.exists(_output_file):
        os.remove(_output_file)

    return annotations

#
# main
#


def _annotate(line, annotations, scroll):
    by_priority = sorted(annotations, reverse=True)

    style = by_priority[0].style.value
    text = ',\n'.join([a.text for a in by_priority])

    editor.annotate_line(line, text, style, True, scroll=scroll)


def _remove_trailing_whitespaces(text):
    return _REMOVE_TRAILING_WHITESPACES_REGEX.sub('', text)


def _remove_trailing_lines(text):
    return _REMOVE_TRAILING_LINES_REGEX.sub('', text)


def _editor_text():
    text = editor.get_text()

    range_end = len(text)

    if _remove_whitespaces():
        text = _remove_trailing_whitespaces(text)
        text = _remove_trailing_lines(text)
        editor.replace_text(0, range_end, text)
        tab.save()
        # Pythonista is adding '\n' automatically, so, if we removed them
        # all we have to simulate Pythonista behavior by adding '\n'
        # for pyflakes & pep8 analysis
        return text + '\n'

    tab.save()
    return text


def main():
    path = editor.get_path()

    if not path:
        return

    if not path.endswith('.py'):
        return

    editor.clear_annotations()

    flake8_options = get_config_value('analyzer.flake8', None)

    selection = editor.get_selection()
    text = _editor_text()

    if flake8_options:
        annotations = _flake8_annotations(
            os.path.abspath(path),
            flake8_options
        )
    else:
        annotations = _pep8_annotations(
            text,
            ignore=_ignore_codes(),
            max_line_length=_max_line_length()
        )

        annotations += _pyflakes_annotations(path, text)

    if not annotations:
        if selection:
            editor.set_selection(selection[0], scroll=True)
        console.hud_alert('No Issues Found', 'iob:checkmark_32', _hud_alert_delay())
        return None

    scroll = True
    by_line = sorted(annotations, key=lambda x: x.line)
    for l, a in groupby(by_line, lambda x: x.line):
        _annotate(l, a, scroll)
        scroll = False


if __name__ == '__main__':
    from blackmamba.bundle import bundle
    with bundle('analyze'):
        main()
