from csv import DictWriter
from io import StringIO
from typing import Any, Dict, Iterable, List, Optional, Union

from flask import Response, jsonify, stream_with_context, request
from flask.json import dumps
import orjson

from ._config import MAX_RESULTS, MAX_COMPATIBILITY_RESULTS
# TODO: remove warnings after once we are past the API_KEY_REQUIRED_STARTING_AT date
from ._security import show_hard_api_key_warning, show_soft_api_key_warning, ROLLOUT_WARNING_RATE_LIMIT, ROLLOUT_WARNING_MULTIPLES, _ROLLOUT_WARNING_AD_FRAGMENT, PHASE_1_2_STOPGAP
from ._common import is_compatibility_mode, log_info_with_request
from ._limiter import requests_left, get_multiples_count
from delphi.epidata.common.logger import get_structured_logger


def print_non_standard(format: str, data):
    """
    prints a non standard JSON message
    """
    if format == "json":
        return jsonify(data)

    if not data:
        message = "no results"
        result = -2
    else:
        if show_hard_api_key_warning():
            message = ""
            if requests_left() == 0:
                message = f"{message} {ROLLOUT_WARNING_RATE_LIMIT}"
            if get_multiples_count(request) < 0:
                message = f"{message} {ROLLOUT_WARNING_MULTIPLES}"
            if requests_left() == 0 or get_multiples_count(request) < 0:
                message = f"{message} {_ROLLOUT_WARNING_AD_FRAGMENT} {PHASE_1_2_STOPGAP}"
        else:
            message = "success"
        result = 1
    if result == -1 and is_compatibility_mode():
        return jsonify(dict(result=result, message=message))
    return jsonify(dict(result=result, message=message, epidata=data))


class APrinter:
    def __init__(self):
        self.count: int = 0
        self.result: int = -1
        self._max_results: int = MAX_COMPATIBILITY_RESULTS if is_compatibility_mode() else MAX_RESULTS

    def make_response(self, gen):
        return Response(
            gen,
            mimetype="application/json",
        )

    def __call__(self, generator: Iterable[Dict[str, Any]]) -> Response:
        def gen():
            self.result = -2  # no result, default response
            began = False
            try:
                for row in generator:
                    if not began:
                        # do it here to catch an error before we send the begin
                        r = self._begin()
                        began = True
                        if r is not None:
                            yield r
                    r = self._print_row(row)
                    if r is not None:
                        yield r
            except Exception as e:
                get_structured_logger('server_error').error("Exception while executing printer", exception=e)
                self.result = -1
                yield self._error(e)

            if not began:
                # do it manually to catch an error before we send the begin
                r = self._begin()
                began = True
                if r is not None:
                    yield r

            r = self._end()
            log_info_with_request("APrinter finished processing rows", count=self.count)
            if r is not None:
                yield r

        return self.make_response(stream_with_context(gen()))

    @property
    def remaining_rows(self) -> int:
        return self._max_results - self.count

    def _begin(self) -> Optional[Union[str, bytes]]:
        # hook
        return None

    def _error(self, error: Exception) -> str:
        # send an generic error
        return dumps(dict(result=self.result, message=f"unknown error occurred: {error}", error=str(error), epidata=[]))

    def _print_row(self, row: Dict) -> Optional[Union[str, bytes]]:
        first = self.count == 0
        if self.count >= self._max_results:
            # hit the limit
            # TODO: consider making this a WARN-level log event
            log_info_with_request("Max result limit reached", count=self.count)
            self.result = 2
            return None
        if first:
            self.result = 1  # at least one row
        self.count += 1
        return self._format_row(first, row)

    def _format_row(self, first: bool, row: Dict) -> Optional[Union[str, bytes]]:
        # hook
        return None

    def _end(self) -> Optional[Union[str, bytes]]:
        # hook
        return None


class ClassicPrinter(APrinter):
    """
    a printer class writing in the classic epidata format
    """

    def _begin(self):
        if is_compatibility_mode() and not show_hard_api_key_warning():
            return "{ "
        r = '{ "epidata": ['
        if show_hard_api_key_warning():
            message = ""
            if requests_left() == 0:
                message = f"{message} {ROLLOUT_WARNING_RATE_LIMIT}"
            if get_multiples_count(request) < 0:
                message = f"{message} {ROLLOUT_WARNING_MULTIPLES}"
            if requests_left() == 0 or get_multiples_count(request) < 0:
                message = f"{message} {_ROLLOUT_WARNING_AD_FRAGMENT} {PHASE_1_2_STOPGAP}"
            return f'{r} "{message}"'
        return r

    def _format_row(self, first: bool, row: Dict):
        if first and is_compatibility_mode() and not show_hard_api_key_warning():
            sep = b'"epidata": ['
        else:
            sep = b"," if not first or (show_hard_api_key_warning() and requests_left() == 0) else b""
        return sep + orjson.dumps(row)

    def _end(self):
        message = ""
        if show_soft_api_key_warning():
            if requests_left() == 0:
                message = f"{message} {ROLLOUT_WARNING_RATE_LIMIT}"
            if get_multiples_count(request) < 0:
                message = f"{message} {ROLLOUT_WARNING_MULTIPLES}"
            if requests_left() == 0 or get_multiples_count(request) < 0:
                message = f"{message} {_ROLLOUT_WARNING_AD_FRAGMENT} {PHASE_1_2_STOPGAP}"
        else:
            message = "success"
        prefix = "], "
        if self.count == 0 and is_compatibility_mode() and not show_hard_api_key_warning():
            # no array to end
            prefix = ""

        if self.count == 0:
            message = "no results"
        elif self.result == 2:
            message = "too many results, data truncated"
        return f'{prefix}"result": {self.result}, "message": {dumps(message)} }}'.encode("utf-8")


class ClassicTreePrinter(ClassicPrinter):
    """
    a printer class writing a tree by the given grouping criteria as the first element in the epidata array
    """

    group: str
    _tree: Dict[str, List[Dict]] = dict()

    def __init__(self, group: str):
        super(ClassicTreePrinter, self).__init__()
        self.group = group

    def _begin(self):
        self._tree = dict()
        return super(ClassicTreePrinter, self)._begin()

    def _format_row(self, first: bool, row: Dict):
        group = row.get(self.group, "")
        del row[self.group]
        if group in self._tree:
            self._tree[group].append(row)
        else:
            self._tree[group] = [row]
        if first and is_compatibility_mode() and not show_hard_api_key_warning():
            return b'"epidata": ['
        return None

    def _end(self):
        if self.count == 0:
            return super(ClassicTreePrinter, self)._end()

        tree = orjson.dumps(self._tree)
        self._tree = dict()
        r = super(ClassicTreePrinter, self)._end()
        r = tree + r
        if show_hard_api_key_warning() and requests_left() == 0:
            r = b", " + r
        return r


class CSVPrinter(APrinter):
    """
    a printer class writing in a CSV file
    """

    _stream = StringIO()
    _writer: DictWriter
    _filename: Optional[str]

    def __init__(self, filename: Optional[str] = "epidata"):
        super(CSVPrinter, self).__init__()
        self._filename = filename

    def make_response(self, gen):
        headers = {"Content-Disposition": f"attachment; filename={self._filename}.csv"} if self._filename else {}
        return Response(gen, mimetype="text/csv; charset=utf8", headers=headers)

    def _begin(self):
        return None

    def _error(self, error: Exception) -> str:
        # send an generic error
        return f"unknown error occurred:\n{error}"

    def _format_row(self, first: bool, row: Dict):
        if first:
            columns = list(row.keys())
            self._writer = DictWriter(self._stream, columns, lineterminator="\n")
            self._writer.writeheader()
            if show_hard_api_key_warning():
                message = ""
                if requests_left() == 0:
                    message = f"{message} {ROLLOUT_WARNING_RATE_LIMIT}"
                if get_multiples_count(request) < 0:
                    message = f"{message} {ROLLOUT_WARNING_MULTIPLES}"
                if requests_left() == 0 or get_multiples_count(request) < 0:
                    message = f"{message} {_ROLLOUT_WARNING_AD_FRAGMENT} {PHASE_1_2_STOPGAP}"
                self._writer.writerow({columns[0]: message})

        self._writer.writerow(row)

        # remove the stream content to print just one line at a time
        self._stream.flush()
        v = self._stream.getvalue()
        self._stream.seek(0)
        self._stream.truncate(0)
        return v

    def _end(self):
        self._writer = None
        return ""


class JSONPrinter(APrinter):
    """
    a printer class writing in a JSON array
    """

    def _begin(self):
        r = b"["
        if show_hard_api_key_warning():
            message = ""
            if requests_left() == 0:
                message = f"{message} {ROLLOUT_WARNING_RATE_LIMIT}"
            if get_multiples_count(request) < 0:
                message = f"{message} {ROLLOUT_WARNING_MULTIPLES}"
            if requests_left() == 0 or get_multiples_count(request) < 0:
                message = f"{message} {_ROLLOUT_WARNING_AD_FRAGMENT} {PHASE_1_2_STOPGAP}"
            r = b'["' + bytes(message, "utf-8") + b'"'
        return r

    def _format_row(self, first: bool, row: Dict):
        sep = b"," if not first or (show_hard_api_key_warning() and requests_left() == 0) else b""
        return sep + orjson.dumps(row)

    def _end(self):
        return b"]"


class JSONLPrinter(APrinter):
    """
    a printer class writing in JSONLines format
    """

    def make_response(self, gen):
        return Response(gen, mimetype=" text/plain; charset=utf8")

    def _begin(self):
        message = ""
        if show_hard_api_key_warning():
            message = ""
            if requests_left() == 0:
                message = f"{message} {ROLLOUT_WARNING_RATE_LIMIT}"
            if get_multiples_count(request) < 0:
                message = f"{message} {ROLLOUT_WARNING_MULTIPLES}"
            if requests_left() == 0 or get_multiples_count(request) < 0:
                message = f"{message} {_ROLLOUT_WARNING_AD_FRAGMENT} {PHASE_1_2_STOPGAP}"
            return bytes(message, "utf-8") + b"\n"
        return None

    def _format_row(self, first: bool, row: Dict):
        # each line is a JSON file with a new line to separate them
        return orjson.dumps(row, option=orjson.OPT_APPEND_NEWLINE)

    def _end(self):
        return b""


def create_printer(format: str) -> APrinter:
    if format is None:
        return ClassicPrinter()
    if format == "tree":
        return ClassicTreePrinter("signal")
    if format.startswith("tree-"):
        # support tree format by any property following the dash
        return ClassicTreePrinter(format[len("tree-") :])
    if format == "json":
        return JSONPrinter()
    if format == "csv":
        return CSVPrinter()
    if format == "jsonl":
        return JSONLPrinter()
    return ClassicPrinter()
