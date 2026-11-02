"""A fake spreadsheet that answers range reads and writes like the Sheets API"""

import re

import gspread


class FakeTab:
    def __init__(self, gid, title, rows, row_count=None):
        self.id, self.title, self.rows = gid, title, rows
        self.row_count = len(rows) if row_count is None else row_count
        self.writes = []

    def batch_update(self, data, value_input_option=None):
        self.writes.append((data, value_input_option))
        for update in data:
            row, col = gspread.utils.a1_to_rowcol(update["range"])
            while len(self.rows) < row:
                self.rows.append([])
            while len(self.rows[row - 1]) < col:
                self.rows[row - 1].append("")
            self.rows[row - 1][col - 1] = update["values"][0][0]


class FakeSpreadsheet:
    def __init__(self, tabs, spreadsheet_id="spreadsheet1", title="Example Sheet"):
        self.id, self.title, self.tabs = spreadsheet_id, title, tabs
        self.ranges_read = []
        self.batch_calls = 0

    def worksheets(self):
        return self.tabs

    def values_batch_get(self, ranges):
        self.batch_calls += 1
        self.ranges_read.extend(ranges)
        return {"valueRanges": [self._read(r) for r in ranges]}

    def _read(self, a1):
        title, cells = re.fullmatch(r"'(.*)'!(.+)", a1).groups()
        tab = next(t for t in self.tabs if t.title == title.replace("''", "'"))
        if m := re.fullmatch(r"(\d+):\d+", cells):
            return {"values": [tab.rows[int(m[1]) - 1]]}
        if m := re.fullmatch(r"([A-Z]+)(\d+)", cells):
            column = gspread.utils.a1_to_rowcol(f"{m[1]}1")[1] - 1
            number = int(m[2])
            row = tab.rows[number - 1] if number <= len(tab.rows) else []
            return {"values": [[row[column]]]} if column < len(row) and row[column] != "" else {}
        letter, start = re.fullmatch(r"([A-Z]+)(\d+):[A-Z]+", cells).groups()
        column = gspread.utils.a1_to_rowcol(f"{letter}1")[1] - 1
        values = [
            [row[column]] if column < len(row) and row[column] != "" else [] for row in tab.rows[int(start) - 1 :]
        ]
        while values and not values[-1]:
            values.pop()
        return {"values": values} if values else {}


def opener_for(spreadsheet):
    return lambda settings, spreadsheet_id=None: spreadsheet
