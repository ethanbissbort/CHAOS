using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// Reading <c>GET /host/setup</c>.
/// </summary>
/// <remarks>
/// This shell and that endpoint are built by different hands, so the reader is
/// liberal about spelling and strict about meaning. The property that matters is
/// one-directional: a field name this shell does not recognise can make it say
/// "I don't know", and can never make it say "ready".
/// </remarks>
public sealed class SetupSnapshotReaderTests
{
    [Fact]
    public void The_documented_shape_is_read()
    {
        var snapshot = SetupSnapshotReader.Read(
            """
            {
              "state": "needs_attention",
              "summary": "Setup completed with two points unbound.",
              "steps": [
                {"id": "database", "label": "Database", "state": "ready", "detail": "Opened var/chaos.db."},
                {"id": "schema", "label": "Schema", "state": "ready", "detail": "At revision 0.4.0."},
                {"id": "bindings", "label": "Bindings", "state": "needs_attention",
                 "detail": "Two points have no driver binding."}
              ]
            }
            """);

        Assert.Equal(SetupAvailability.Available, snapshot.Availability);
        Assert.Equal(SetupState.NeedsAttention, snapshot.State);
        Assert.Equal("Setup completed with two points unbound.", snapshot.Summary);
        Assert.Equal(3, snapshot.Steps.Count);
        Assert.Equal("Database", snapshot.Steps[0].Label);
        Assert.Equal(SetupStepState.NeedsAttention, snapshot.Steps[2].State);
        Assert.True(snapshot.NeedsOperator);
        Assert.False(snapshot.IsReady);
    }

    /// <summary>
    /// The body the gateway actually sends, field for field, including the ones
    /// this shell did not originally expect.
    /// </summary>
    [Fact]
    public void The_gateways_own_payload_shape_is_read()
    {
        var snapshot = SetupSnapshotReader.Read(
            """
            {
              "state": "needs_attention",
              "stateExplanation": "Setup completed but something needs a person to look at it.",
              "summary": "The database is present but two points have no binding.",
              "autoSetupEnabled": true,
              "inProgress": false,
              "setupRequired": true,
              "runRequiresForce": true,
              "steps": [
                {"id": "database", "title": "Database", "state": "present",
                 "stateExplanation": "Checked, and there.", "detail": "Opened var/chaos.db.",
                 "count": null, "value": "sqlite:///var/chaos.db"},
                {"id": "bindings", "title": "Bindings", "state": "partial",
                 "stateExplanation": "Checked, and there but incomplete.", "detail": null,
                 "count": 2, "value": null},
                {"id": "load_schedule", "title": "Load schedule", "state": "absent",
                 "stateExplanation": "Checked, and not there.", "detail": null}
              ],
              "failure": null
            }
            """);

        Assert.Equal(SetupAvailability.Available, snapshot.Availability);
        Assert.Equal(SetupState.NeedsAttention, snapshot.State);
        Assert.True(snapshot.RunRequiresForce);
        Assert.Equal("The database is present but two points have no binding.", snapshot.Summary);

        Assert.Equal(new[] { "Database", "Bindings", "Load schedule" },
            snapshot.Steps.Select(s => s.Label));

        Assert.Equal(
            new[] { SetupStepState.Ready, SetupStepState.Partial, SetupStepState.Absent },
            snapshot.Steps.Select(s => s.State));

        // A null detail falls back to the gateway's own sentence for the state,
        // never to this shell's generic wording.
        Assert.Equal("Checked, and there but incomplete.", snapshot.Steps[1].Detail);
    }

    [Theory]
    [InlineData("unknown", SetupStepState.Unknown)]
    [InlineData("absent", SetupStepState.Absent)]
    [InlineData("partial", SetupStepState.Partial)]
    [InlineData("present", SetupStepState.Ready)]
    [InlineData("running", SetupStepState.Running)]
    [InlineData("failed", SetupStepState.Failed)]
    public void Every_step_state_in_the_gateways_vocabulary_is_understood(
        string word, SetupStepState expected) =>
        Assert.Equal(expected, SetupSnapshotReader.ParseStepState(word));

    [Fact]
    public void A_force_flag_the_gateway_did_not_send_is_read_as_not_required()
    {
        // Forcing a run the gateway wanted a human decision about is exactly
        // what its guard exists to prevent, so absence never means "go ahead".
        Assert.False(SetupSnapshotReader.Read("""{"state": "ready"}""").RunRequiresForce);
    }

    [Theory]
    [InlineData("not_started", SetupState.NotStarted)]
    [InlineData("checking", SetupState.Checking)]
    [InlineData("running", SetupState.Running)]
    [InlineData("ready", SetupState.Ready)]
    [InlineData("failed", SetupState.Failed)]
    [InlineData("needs_attention", SetupState.NeedsAttention)]
    public void Every_state_in_the_contract_is_understood(string word, SetupState expected) =>
        Assert.Equal(expected, SetupSnapshotReader.ParseState(word));

    [Theory]
    [InlineData("NOT_STARTED")]
    [InlineData("Not-Started")]
    [InlineData("notStarted")]
    [InlineData("  not_started  ")]
    public void Spelling_variations_of_a_state_are_tolerated(string word) =>
        Assert.Equal(SetupState.NotStarted, SetupSnapshotReader.ParseState(word));

    [Theory]
    [InlineData("almost")]
    [InlineData("")]
    [InlineData(null)]
    [InlineData("readyish")]
    public void An_unrecognised_state_is_unknown_and_never_ready(string? word)
    {
        var state = SetupSnapshotReader.ParseState(word);

        Assert.Equal(SetupState.Unknown, state);
        Assert.NotEqual(SetupState.Ready, state);
    }

    [Theory]
    [InlineData("something-new")]
    [InlineData("")]
    [InlineData(null)]
    public void An_unrecognised_step_state_is_unknown_and_never_ready(string? word)
    {
        var state = SetupSnapshotReader.ParseStepState(word);

        Assert.Equal(SetupStepState.Unknown, state);
        Assert.NotEqual(SetupStepState.Ready, state);
    }

    [Fact]
    public void The_alternative_field_names_a_host_might_use_are_accepted()
    {
        var snapshot = SetupSnapshotReader.Read(
            """
            {
              "status": "ready",
              "message": "All good.",
              "checks": [
                {"key": "database", "status": "ok", "explanation": "Present."}
              ]
            }
            """);

        Assert.Equal(SetupState.Ready, snapshot.State);
        Assert.Equal("All good.", snapshot.Summary);
        Assert.Equal(SetupStepState.Ready, snapshot.Steps[0].State);
        Assert.Equal("Present.", snapshot.Steps[0].Detail);
    }

    [Fact]
    public void Steps_reported_as_a_map_are_read_as_well_as_an_array()
    {
        var snapshot = SetupSnapshotReader.Read(
            """
            {
              "state": "running",
              "steps": {
                "database": {"state": "ready", "detail": "Created."},
                "points": {"state": "running", "detail": "Loading 412 points."}
              }
            }
            """);

        Assert.Equal(2, snapshot.Steps.Count);
        Assert.Equal("database", snapshot.Steps[0].Id);
        Assert.Equal("Database", snapshot.Steps[0].Label);
        Assert.Equal(SetupStepState.Running, snapshot.Steps[1].State);
    }

    [Fact]
    public void The_terse_map_form_is_read()
    {
        var snapshot = SetupSnapshotReader.Read(
            """{"state": "ready", "steps": {"database": "ready", "points": "ready"}}""");

        Assert.Equal(2, snapshot.Steps.Count);
        Assert.All(snapshot.Steps, s => Assert.Equal(SetupStepState.Ready, s.State));
    }

    [Fact]
    public void Steps_are_shown_in_the_documented_order_whatever_order_they_arrive_in()
    {
        var snapshot = SetupSnapshotReader.Read(
            """
            {
              "state": "ready",
              "steps": [
                {"id": "load_schedule", "state": "ready"},
                {"id": "database", "state": "ready"},
                {"id": "alarm_definitions", "state": "ready"},
                {"id": "schema", "state": "ready"}
              ]
            }
            """);

        Assert.Equal(
            new[] { "database", "schema", "alarm_definitions", "load_schedule" },
            snapshot.Steps.Select(s => s.Id));
    }

    [Fact]
    public void A_step_this_shell_has_never_heard_of_is_still_shown()
    {
        // A setup step added on the host side must not become invisible just
        // because this shell has not been rebuilt.
        var snapshot = SetupSnapshotReader.Read(
            """
            {
              "state": "running",
              "steps": [
                {"id": "weather_feed", "state": "running", "detail": "Fetching."},
                {"id": "database", "state": "ready"}
              ]
            }
            """);

        Assert.Equal(new[] { "database", "weather_feed" }, snapshot.Steps.Select(s => s.Id));
        Assert.Equal("Weather feed", snapshot.Steps[1].Label);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json at all")]
    [InlineData("[]")]
    [InlineData("null")]
    [InlineData("{\"steps\": []}")]
    public void An_unreadable_body_is_unreadable_and_never_ready(string body)
    {
        var snapshot = SetupSnapshotReader.Read(body);

        Assert.Equal(SetupAvailability.Unreadable, snapshot.Availability);
        Assert.False(snapshot.IsReady);
        Assert.False(string.IsNullOrWhiteSpace(snapshot.Problem));
    }

    [Fact]
    public void A_missing_endpoint_is_reported_as_an_old_host_not_a_fault()
    {
        var snapshot = SetupSnapshot.Absent("HTTP 404 from http://node:8080/host/setup");

        Assert.True(snapshot.HostTooOld);
        Assert.False(snapshot.IsReady);
        Assert.Contains("older than", snapshot.Problem!, StringComparison.Ordinal);

        // It must not imply the platform is unset up or broken.
        Assert.Contains("may be perfectly set up", snapshot.Problem!, StringComparison.Ordinal);
    }
}

/// <summary>Turning the snapshot into a panel.</summary>
public sealed class SetupPresenterTests
{
    private static SetupSnapshot Parse(string json) => SetupSnapshotReader.Read(json);

    [Fact]
    public void An_unset_up_platform_gets_a_set_up_now_button_as_the_main_thing_to_do()
    {
        var view = SetupPresenter.Present(Parse("""{"state": "not_started"}"""));

        Assert.Equal(SetupPresenter.SetUpNow, view.RunLabel);
        Assert.True(view.RunIsPrimary);
        Assert.Equal(CheckState.Fail, view.OverallState);
    }

    [Fact]
    public void A_failed_setup_gets_a_retry_and_names_the_failing_step()
    {
        var view = SetupPresenter.Present(Parse(
            """
            {"state": "failed", "steps": [
              {"id": "points", "state": "failed", "detail": "points.yaml line 40: unknown asset."}]}
            """));

        Assert.Equal(SetupPresenter.RetrySetup, view.RunLabel);
        Assert.True(view.RunIsPrimary);
        Assert.Contains("unknown asset", view.Summary, StringComparison.Ordinal);
        Assert.Single(view.Problems);
    }

    [Fact]
    public void A_ready_platform_can_still_be_re_checked_but_is_not_asked_to_be()
    {
        var view = SetupPresenter.Present(Parse("""{"state": "ready"}"""));

        Assert.Equal(SetupPresenter.RunAgain, view.RunLabel);
        Assert.False(view.RunIsPrimary);
        Assert.Equal(CheckState.Pass, view.OverallState);
    }

    [Fact]
    public void While_setup_runs_no_button_is_offered_at_all()
    {
        foreach (var state in new[] { "running", "checking" })
        {
            var view = SetupPresenter.Present(Parse($$"""{"state": "{{state}}"}"""));

            Assert.Null(view.RunLabel);
            Assert.Equal(CheckState.Checking, view.OverallState);
        }
    }

    [Fact]
    public void A_state_this_shell_cannot_read_is_never_drawn_as_a_pass()
    {
        foreach (var snapshot in new[]
        {
            SetupSnapshot.NotChecked,
            SetupSnapshot.Absent("404"),
            SetupSnapshot.Unreachable("refused"),
            SetupSnapshot.Unreadable("garbage"),
            Parse("""{"state": "mystery"}"""),
        })
        {
            var view = SetupPresenter.Present(snapshot);

            Assert.Equal(CheckState.Unknown, view.OverallState);
            Assert.Null(view.RunLabel);
            Assert.False(string.IsNullOrWhiteSpace(view.Headline));
            Assert.False(string.IsNullOrWhiteSpace(view.Summary));
        }
    }

    [Fact]
    public void A_step_that_said_nothing_still_gets_a_sentence()
    {
        var view = SetupPresenter.Present(Parse(
            """{"state": "failed", "steps": [{"id": "schema", "state": "failed"}]}"""));

        Assert.All(view.Rows, row => Assert.False(string.IsNullOrWhiteSpace(row.Detail)));
        Assert.Contains("check the platform log", view.Rows[0].Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void Step_states_map_to_drawing_states_without_flattering_any_of_them()
    {
        var view = SetupPresenter.Present(Parse(
            """
            {"state": "needs_attention", "steps": [
              {"id": "database", "state": "ready"},
              {"id": "schema", "state": "pending"},
              {"id": "assets", "state": "running"},
              {"id": "points", "state": "failed"},
              {"id": "bindings", "state": "needs_attention"},
              {"id": "load_schedule", "state": "skipped"},
              {"id": "extra", "state": "who-knows"}]}
            """));

        Assert.Equal(
            new[]
            {
                CheckState.Pass, CheckState.Pending, CheckState.Checking, CheckState.Fail,
                CheckState.Warn, CheckState.NotApplicable, CheckState.Unknown,
            },
            view.Rows.Select(r => r.State));
    }
}
