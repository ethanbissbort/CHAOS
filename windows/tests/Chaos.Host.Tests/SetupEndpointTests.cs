using System.Net;
using System.Text.Json;
using Chaos.Host.Setup;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// <c>GET /host/setup</c>, <c>POST /host/setup/run</c> and the setup dimension
/// of <c>/health</c>.
/// </summary>
/// <remarks>
/// The shell renders this payload, so the field names and wire values asserted
/// here are a contract rather than an implementation detail.
/// </remarks>
public sealed class SetupEndpointTests
{
    [Fact]
    public async Task Host_setup_reports_not_started_on_a_node_that_manages_itself()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        using var response = await client.GetAsync("/host/setup");

        // Always 200. "This machine needs setting up" is a successful report,
        // not a failed request.
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var body = await response.ReadJsonAsync();
        Assert.Equal("not_started", body.GetStringProperty("state"));
        Assert.False(body.GetProperty("autoSetupEnabled").GetBoolean());
        Assert.False(body.GetProperty("inProgress").GetBoolean());
        Assert.True(body.GetProperty("runRequiresForce").GetBoolean());
        Assert.Equal(0, body.GetProperty("runCount").GetInt32());

        // Nothing has been read, so nothing is claimed either way.
        Assert.Equal(JsonValueKind.Null, body.GetProperty("lastCheckedUtc").ValueKind);
        Assert.Equal(JsonValueKind.Null, body.GetProperty("currentActivity").ValueKind);
        Assert.Equal(JsonValueKind.Null, body.GetProperty("failure").ValueKind);
        Assert.Empty(body.GetProperty("steps").EnumerateArray());

        Assert.False(string.IsNullOrWhiteSpace(body.GetStringProperty("stateExplanation")));
        Assert.Equal("/host/setup/run", body.GetProperty("actions").GetProperty("run").GetStringProperty("path"));
        Assert.Equal("POST", body.GetProperty("actions").GetProperty("run").GetStringProperty("method"));
    }

    [Fact]
    public async Task A_fresh_machine_is_set_up_at_startup_and_reported_step_by_step()
    {
        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var factory = SetupHostFactory.For(runner).Build();
        using var client = factory.CreateClient();

        await TestExtensions.WaitUntilAsync(
            async () => (await ReadSetupAsync(client)).GetStringProperty("state") == "ready",
            "setup to reach 'ready' after starting on a fresh machine");

        var body = await ReadSetupAsync(client);
        Assert.Equal("startup", body.GetStringProperty("trigger"));
        Assert.Equal(1, body.GetProperty("runCount").GetInt32());
        Assert.False(body.GetProperty("setupRequired").GetBoolean());

        var steps = body.GetProperty("steps").EnumerateArray()
            .ToDictionary(step => step.GetStringProperty("id"), step => step);

        Assert.Equal(SetupStepIds.Ordered, [.. body.GetProperty("steps").EnumerateArray()
            .Select(step => step.GetStringProperty("id"))]);

        Assert.Equal("present", steps[SetupStepIds.Schema].GetStringProperty("state"));
        Assert.Equal(90, steps[SetupStepIds.Assets].GetProperty("count").GetInt32());
        Assert.Equal(701, steps[SetupStepIds.Points].GetProperty("count").GetInt32());
        Assert.Equal(245, steps[SetupStepIds.Bindings].GetProperty("count").GetInt32());
        Assert.Equal(40, steps[SetupStepIds.AlarmDefinitions].GetProperty("count").GetInt32());
        Assert.Equal(12, steps[SetupStepIds.LoadSchedule].GetProperty("count").GetInt32());

        Assert.All(steps.Values, step =>
        {
            Assert.False(string.IsNullOrWhiteSpace(step.GetStringProperty("detail")));
            Assert.False(string.IsNullOrWhiteSpace(step.GetStringProperty("stateExplanation")));
        });

        // Every command the gateway ran, as an operator would type it.
        var commands = body.GetProperty("commands").EnumerateArray()
            .Select(command => command.GetStringProperty("command"))
            .ToList();
        Assert.Equal(4, commands.Count);
        Assert.Contains(commands, command => command.Contains("init-db", StringComparison.Ordinal));
        Assert.Contains(commands, command => command.Contains("load-all --skip-missing", StringComparison.Ordinal));
        Assert.All(commands, command => Assert.Contains("chaos.cli", command, StringComparison.Ordinal));

        var configuration = body.GetProperty("configuration");
        Assert.True(configuration.GetProperty("autoSetup").GetBoolean());
        Assert.Contains("chaos", configuration.GetStringProperty("databaseUrl"), StringComparison.Ordinal);
        Assert.False(string.IsNullOrWhiteSpace(configuration.GetStringProperty("logPath")));
    }

    [Fact]
    public async Task Post_setup_run_starts_a_run_and_answers_202()
    {
        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var factory = SetupHostFactory.For(runner).WithAutoSetup(false).Build();
        using var client = factory.CreateClient();

        // Auto-setup off, so an unforced request is declined and nothing runs.
        using (var declined = await client.PostAsync("/host/setup/run", content: null))
        {
            Assert.Equal(HttpStatusCode.OK, declined.StatusCode);
            var body = await declined.ReadJsonAsync();
            Assert.False(body.GetProperty("accepted").GetBoolean());
            Assert.Equal("auto_setup_disabled", body.GetStringProperty("reason"));
            Assert.Equal("not_started", body.GetProperty("setup").GetStringProperty("state"));
        }

        Assert.Empty(runner.Invocations);

        using (var accepted = await client.PostAsync("/host/setup/run?force=true", content: null))
        {
            Assert.Equal(HttpStatusCode.Accepted, accepted.StatusCode);
            var body = await accepted.ReadJsonAsync();
            Assert.True(body.GetProperty("accepted").GetBoolean());
            Assert.Equal("started", body.GetStringProperty("reason"));
            Assert.True(body.GetProperty("forced").GetBoolean());
        }

        await TestExtensions.WaitUntilAsync(
            async () => (await ReadSetupAsync(client)).GetStringProperty("state") == "ready",
            "a forced run to finish");

        var final = await ReadSetupAsync(client);
        Assert.Equal("api", final.GetStringProperty("trigger"));
    }

    [Fact]
    public async Task Two_calls_to_setup_run_do_not_start_two_setups()
    {
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        })
        {
            Gate = gate,
        };

        // Auto-setup off so the only runs are the ones this test asks for.
        using var factory = SetupHostFactory.For(runner).WithAutoSetup(false).Build();
        using var client = factory.CreateClient();

        using var first = await client.PostAsync("/host/setup/run?force=true", content: null);
        Assert.Equal(HttpStatusCode.Accepted, first.StatusCode);

        await runner.Entered.Task.WaitAsync(TimeSpan.FromSeconds(10));

        using var second = await client.PostAsync("/host/setup/run?force=true", content: null);
        Assert.Equal(HttpStatusCode.OK, second.StatusCode);

        var body = await second.ReadJsonAsync();
        Assert.False(body.GetProperty("accepted").GetBoolean());
        Assert.Equal("already_running", body.GetStringProperty("reason"));

        // The declined call still carries the live report, so the shell can
        // render progress from one round trip.
        var setup = body.GetProperty("setup");
        Assert.True(setup.GetProperty("inProgress").GetBoolean());
        Assert.False(string.IsNullOrWhiteSpace(setup.GetStringProperty("currentActivity")));

        Assert.Equal(1, factory.Coordinator.RunsStarted);

        runner.Gate = null;
        gate.SetResult();

        await TestExtensions.WaitUntilAsync(
            async () => (await ReadSetupAsync(client)).GetStringProperty("state") == "ready",
            "the single run to finish");

        Assert.Equal(1, factory.Coordinator.Snapshot.RunCount);
        Assert.Single(runner.Invocations, line => line == "init-db");
    }

    [Fact]
    public async Task A_failure_is_reported_with_the_command_the_exit_code_and_the_log()
    {
        var runner = new FakePlatformCommandRunner((verb, _) => verb switch
        {
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "init-db" => FakePlatformCommandRunner.Fails(
                1,
                "The platform CLI exited 1 (a runtime failure).\nchaos: error: unable to open database file",
                "chaos: error: unable to open database file"),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var factory = SetupHostFactory.For(runner).Build();
        using var client = factory.CreateClient();

        await TestExtensions.WaitUntilAsync(
            async () => (await ReadSetupAsync(client)).GetStringProperty("state") == "failed",
            "setup to report a failure");

        var failure = (await ReadSetupAsync(client)).GetProperty("failure");
        Assert.Equal("schema", failure.GetStringProperty("step"));
        Assert.Equal(1, failure.GetProperty("exitCode").GetInt32());
        Assert.Contains("init-db", failure.GetStringProperty("command"), StringComparison.Ordinal);
        Assert.Contains("unable to open database file", failure.GetStringProperty("message"),
            StringComparison.Ordinal);
        Assert.False(string.IsNullOrWhiteSpace(failure.GetStringProperty("remedy")));
        Assert.False(string.IsNullOrWhiteSpace(failure.GetStringProperty("logPath")));
        Assert.NotEmpty(failure.GetProperty("standardErrorTail").EnumerateArray());

        // A failed run is retryable and the shell is told so.
        var body = await ReadSetupAsync(client);
        Assert.True(body.GetProperty("setupRequired").GetBoolean());
        Assert.False(body.GetProperty("inProgress").GetBoolean());
    }

    [Fact]
    public async Task Health_carries_the_setup_state_without_changing_its_existing_shape()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        await TestExtensions.WaitUntilAsync(
            async () => (await (await client.GetAsync("/health")).ReadJsonAsync())
                .GetStringProperty("backend") == "up",
            "the stub backend to be reported up");

        using var response = await client.GetAsync("/health");
        var body = await response.ReadJsonAsync();

        // Auto-setup is off in this factory, so setup says nothing about the
        // database and must not colour health either way.
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("ok", body.GetStringProperty("status"));

        // The pre-existing fields are all still exactly where they were.
        Assert.Equal("up", body.GetStringProperty("backend"));
        Assert.Equal("Chaos.Host", body.GetProperty("host").GetStringProperty("component"));
        Assert.Equal(backend.Url, body.GetProperty("backendDetail").GetStringProperty("url"));
        Assert.False(body.GetProperty("supervisor").GetProperty("registered").GetBoolean());

        var setup = body.GetProperty("setup");
        Assert.Equal("not_started", setup.GetStringProperty("state"));
        Assert.False(setup.GetProperty("autoSetupEnabled").GetBoolean());
        Assert.Equal("/host/setup", setup.GetStringProperty("detail"));
        Assert.Equal(JsonValueKind.Null, setup.GetProperty("failure").ValueKind);
    }

    [Fact]
    public async Task Health_refuses_to_report_ok_over_a_platform_with_no_database()
    {
        var runner = new FakePlatformCommandRunner((verb, _) => verb switch
        {
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "init-db" => FakePlatformCommandRunner.Fails(1, "chaos: error: unable to open database file"),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var factory = SetupHostFactory.For(runner).Build();
        using var client = factory.CreateClient();

        await TestExtensions.WaitUntilAsync(
            async () => (await ReadSetupAsync(client)).GetStringProperty("state") == "failed",
            "setup to fail");

        using var response = await client.GetAsync("/health");
        var body = await response.ReadJsonAsync();

        // This is the failure the project keeps designing against: a gateway
        // that answers 200/ok while the platform behind it has no database.
        Assert.Equal(HttpStatusCode.ServiceUnavailable, response.StatusCode);
        Assert.Equal("degraded", body.GetStringProperty("status"));
        Assert.Equal("failed", body.GetProperty("setup").GetStringProperty("state"));
        Assert.Contains("unable to open database file",
            body.GetProperty("setup").GetStringProperty("failure"), StringComparison.Ordinal);

        // /health/live is unaffected: this process is alive and says only that.
        using var live = await client.GetAsync("/health/live");
        Assert.Equal(HttpStatusCode.OK, live.StatusCode);
    }

    [Fact]
    public async Task Host_routes_lists_the_setup_endpoints_under_the_host_prefix()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        var body = await (await client.GetAsync("/host/routes")).ReadJsonAsync();
        var host = body.GetProperty("routes").EnumerateArray()
            .Single(row => row.GetStringProperty("pathPrefix") == "/host");

        var endpoints = host.GetProperty("nativeEndpoints").EnumerateArray()
            .Select(element => element.GetString())
            .ToList();

        // A .NET-owned prefix has to name the endpoints actually behind it, or
        // "ported" is an unbacked claim.
        Assert.Contains("/host/setup", endpoints);
        Assert.Contains("/host/setup/run", endpoints);
    }

    private static async Task<JsonElement> ReadSetupAsync(HttpClient client)
    {
        using var response = await client.GetAsync("/host/setup");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        return await response.ReadJsonAsync();
    }
}
