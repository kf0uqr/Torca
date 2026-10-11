import ft8_qso as q


def new():
    return q.Qso("KF0UQR", "EM17")


def test_parse_classifies_the_standard_exchange():
    p = q.parse
    assert p("CQ W1AW FN31") == q.Parsed(q.CQ, None, "W1AW", "FN31")
    assert p("CQ DX W1AW FN31") == q.Parsed(q.CQ, None, "W1AW", "FN31")
    assert p("KF0UQR W1AW FN31") == q.Parsed(q.CALL, "KF0UQR", "W1AW", "FN31")
    assert p("KF0UQR W1AW -05").kind == q.REPORT and p("KF0UQR W1AW R-05") == q.Parsed(q.R_REPORT, "KF0UQR", "W1AW", "-05")
    assert p("KF0UQR W1AW RR73").kind == q.RR73          # not mistaken for the grid RR73
    assert p("KF0UQR W1AW RRR").kind == q.RRR and p("KF0UQR W1AW 73").kind == q.SIGN_OFF
    assert p("TNX 73 GL") is None and p("<...> W1AW -05") is None


def test_grid_and_report_formatting():
    assert q.latlon_to_grid(38.2, -97.5) == "EM18"
    assert q.latlon_to_grid(38.2, -97.5, 6) == "EM18ge"
    assert [q.format_report(x) for x in (-10.4, 5, 0, 99, -99)] == ["-10", "+05", "+00", "+32", "-30"]


def test_answering_a_cq_runs_the_whole_exchange():
    qso = new()
    qso.work(q.parse("CQ W1AW FN31"), -7, slot_parity=0)
    assert qso.tx_parity == 1 and qso.message() == "W1AW KF0UQR EM17"
    assert not qso.sent()
    assert qso.heard(q.parse("KF0UQR W1AW -12"), -8, 0)               # they give us a report
    assert qso.rst_rcvd == "-12" and qso.message() == "W1AW KF0UQR R-08"
    qso.sent()
    assert qso.heard(q.parse("KF0UQR W1AW RR73"), -8, 0)
    assert qso.message() == "W1AW KF0UQR 73"
    assert qso.sent() is True and qso.complete                           # log it
    assert qso.message() is None                                          # and we are done


def test_calling_cq_and_being_answered():
    qso = new()
    qso.call_cq(parity=0)
    assert qso.message() == "CQ KF0UQR EM17"
    qso.sent(); qso.sent()                                                # CQ simply repeats, never gives up
    assert not qso.gave_up
    assert qso.heard(q.parse("KF0UQR W1AW FN31"), -3, 0)
    assert qso.dx_call == "W1AW" and qso.dx_grid == "FN31" and qso.tx_parity == 1
    assert qso.message() == "W1AW KF0UQR -03"
    qso.sent()
    assert qso.heard(q.parse("KF0UQR W1AW R-10"), -3, 0)
    assert qso.rst_rcvd == "-10" and qso.message() == "W1AW KF0UQR RR73"
    assert qso.sent() is True
    qso.after_complete()                                                  # started from CQ: back to CQ
    assert qso.message() == "CQ KF0UQR EM17" and qso.dx_call is None


def test_other_stations_are_ignored_during_a_qso():
    qso = new()
    qso.work(q.parse("CQ W1AW FN31"), -7, 0)
    assert not qso.heard(q.parse("KF0UQR N0XYZ -05"), -5, 0)
    assert not qso.heard(q.parse("W1AW N0XYZ -05"), -5, 0)
    assert qso.stage == 1


def test_gives_up_after_too_many_unanswered_tries():
    qso = new()
    qso.work(q.parse("CQ W1AW FN31"), -7, 0)
    for _ in range(q.MAX_TRIES):
        assert qso.message() is not None
        qso.sent()
    assert qso.gave_up and qso.message() is None


def test_clicking_a_station_that_called_us_starts_at_the_right_message():
    qso = new()
    qso.work(q.parse("KF0UQR W1AW FN31"), -4, 1)
    assert qso.message() == "W1AW KF0UQR -04" and qso.tx_parity == 0
    qso.work(q.parse("KF0UQR W1AW -09"), -4, 1)
    assert qso.message() == "W1AW KF0UQR R-04" and qso.rst_rcvd == "-09"
    qso.work(q.parse("KF0UQR W1AW R-09"), -4, 1)
    assert qso.message() == "W1AW KF0UQR RR73"
    qso.work(q.parse("KF0UQR W1AW RR73"), -4, 1)
    assert qso.message() == "W1AW KF0UQR 73"


def test_a_late_73_completes_the_contact():
    qso = new()
    qso.work(q.parse("KF0UQR W1AW R-09"), -4, 1)
    qso.sent()
    assert qso.complete
    assert qso.heard(q.parse("KF0UQR W1AW 73"), -4, 1) and qso.message() is None
