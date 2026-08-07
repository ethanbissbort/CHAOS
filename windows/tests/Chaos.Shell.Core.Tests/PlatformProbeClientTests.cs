using System.Net;
using System.Text;
using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>Answers a request however the test wants.</summary>
internal sealed class StubHandler : HttpMessageHandler
{
    private readonly Func<HttpRequestMessage, HttpResponseMessage> _respond;

    public StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) => _respond = respond;

    public List<HttpRequestMessage> Requests { get; } = new();

    public static StubHandler Returning(HttpStatusCode status, string body) =>
        new(_ => new HttpResponseMessage(status)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        });

    public static StubHandler Throwing(Exception exception) => new(_ => throw exception);

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        Requests.Add(request);
        return Task.FromResult(_respond(request));
    }
}

/// <summary>
/// The shell's HTTP conversation with the gateway.
/// </summary>
/// <remarks>
/// Every judgement here changes what an operator is told, and each is exercised
/// rather than discovered on a node: a 503 is a reachable gateway with a dead
/// backend, a 404 on setup is an old host rather than a broken one, and a
/// timeout is a sentence rather than "the operation was cancelled".
/// </remarks>
public sealed class PlatformProbeClientTests
{
    private static readonly HostEndpoints Endpoints =
        HostEndpoints.For(new Uri("http://chaos-node:8080/"));

    private static PlatformProbeClient Client(StubHandler handler, TimeSpan? timeout = null) =>
        new(new HttpClient(handler) { Timeout = timeout ?? TimeSpan.FromSeconds(4) });

    private const string HealthyBody =
        """
        {"status":"ok","host":{"component":"Chaos.Host","version":"0.5.0"},
         "backend":"up","backendDetail":{"detail":"Backend answered in 4 ms."}}
        """;

    private const string BackendDownBody =
        """
        {"status":"degraded","host":{"version":"0.5.0"},
         "backend":"down","backendDetail":{"detail":"Connection refused on 127.0.0.1:8081."}}
        """;

    // ------------------------------------------------------------- gateway --

    [Fact]
    public async Task A_healthy_gateway_is_read_including_its_backend()
    {
        var probe = await Client(StubHandler.Returning(HttpStatusCode.OK, HealthyBody))
            .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.True(probe.Reachable);
        Assert.Equal(200, probe.StatusCode);
        Assert.Equal(BackendState.Up, probe.Backend);
        Assert.Equal("0.5.0", probe.HostVersion);
        Assert.Equal("Backend answered in 4 ms.", probe.Health!.BackendDetail);
    }

    [Fact]
    public async Task A_503_is_a_reachable_gateway_with_a_dead_backend_not_a_missing_gateway()
    {
        // Treating this as "no gateway" would send an operator hunting for a
        // process that is running fine, and hide the one that is not.
        var probe = await Client(StubHandler.Returning(HttpStatusCode.ServiceUnavailable, BackendDownBody))
            .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.True(probe.Reachable);
        Assert.Equal(503, probe.StatusCode);
        Assert.Equal(BackendState.Down, probe.Backend);
        Assert.Contains("Connection refused", probe.Health!.BackendDetail!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_transport_failure_is_the_only_thing_that_counts_as_unreachable()
    {
        var probe = await Client(StubHandler.Throwing(
                new HttpRequestException("boom", new System.Net.Sockets.SocketException(111))))
            .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.False(probe.Reachable);
        Assert.Equal(BackendState.Unknown, probe.Backend);
        Assert.Equal(1, probe.ConsecutiveFailures);
    }

    [Fact]
    public async Task The_innermost_error_is_reported_because_it_is_the_one_that_says_why()
    {
        var probe = await Client(StubHandler.Throwing(new HttpRequestException(
                "An error occurred while sending the request.",
                new IOException("No connection could be made because the target machine actively refused it."))))
            .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.Contains("actively refused", probe.Error!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_timeout_is_reported_as_a_timeout_not_as_a_cancellation()
    {
        var probe = await Client(
                StubHandler.Throwing(new TaskCanceledException("A task was canceled.")),
                TimeSpan.FromSeconds(4))
            .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.False(probe.Reachable);
        Assert.Contains("no answer within 4 s", probe.Error!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Failures_accumulate_and_the_last_success_is_remembered()
    {
        var previous = GatewayProbe.Answered(null, 200, DateTimeOffset.UnixEpoch);
        var client = Client(StubHandler.Throwing(new HttpRequestException("refused")));

        var first = await client.ProbeGatewayAsync(Endpoints, previous, DateTimeOffset.UnixEpoch);
        var second = await client.ProbeGatewayAsync(Endpoints, first, DateTimeOffset.UnixEpoch);

        Assert.Equal(1, first.ConsecutiveFailures);
        Assert.Equal(2, second.ConsecutiveFailures);
        Assert.Equal(DateTimeOffset.UnixEpoch, second.LastSuccessUtc);
    }

    [Fact]
    public async Task A_body_that_is_not_health_still_counts_as_a_reachable_gateway()
    {
        // Something is listening on that port. Saying "nothing is there" would
        // be wrong, and the check row can say the answer was unreadable.
        var probe = await Client(StubHandler.Returning(HttpStatusCode.OK, "<html>hello</html>"))
            .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.True(probe.Reachable);
        Assert.Null(probe.Health);
        Assert.Equal(BackendState.Unknown, probe.Backend);
    }

    [Fact]
    public async Task The_readiness_probe_asks_the_health_endpoint()
    {
        var handler = StubHandler.Returning(HttpStatusCode.OK, HealthyBody);
        await Client(handler).ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch);

        Assert.Equal(Endpoints.Health, handler.Requests[0].RequestUri);
    }

    [Fact]
    public async Task A_cancelled_probe_propagates_rather_than_being_recorded_as_a_failure()
    {
        using var cancellation = new CancellationTokenSource();
        await cancellation.CancelAsync();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            Client(StubHandler.Throwing(new OperationCanceledException()))
                .ProbeGatewayAsync(Endpoints, GatewayProbe.NotProbed, DateTimeOffset.UnixEpoch, cancellation.Token));
    }

    // --------------------------------------------------------------- setup --

    [Fact]
    public async Task The_setup_report_is_read()
    {
        var snapshot = await Client(StubHandler.Returning(
                HttpStatusCode.OK,
                """{"state":"ready","steps":[{"id":"database","state":"ready","detail":"Present."}]}"""))
            .ReadSetupAsync(Endpoints);

        Assert.Equal(SetupAvailability.Available, snapshot.Availability);
        Assert.True(snapshot.IsReady);
    }

    [Fact]
    public async Task A_503_setup_report_is_still_a_report()
    {
        // The endpoint answers 503 while setup is incomplete, which is exactly
        // the state the launcher came to read.
        var snapshot = await Client(StubHandler.Returning(
                HttpStatusCode.ServiceUnavailable, """{"state":"not_started"}"""))
            .ReadSetupAsync(Endpoints);

        Assert.Equal(SetupAvailability.Available, snapshot.Availability);
        Assert.Equal(SetupState.NotStarted, snapshot.State);
    }

    [Theory]
    [InlineData(HttpStatusCode.NotFound)]
    [InlineData(HttpStatusCode.MethodNotAllowed)]
    [InlineData(HttpStatusCode.NotImplemented)]
    public async Task A_host_without_the_endpoint_is_old_rather_than_broken(HttpStatusCode status)
    {
        var snapshot = await Client(StubHandler.Returning(status, "Not Found")).ReadSetupAsync(Endpoints);

        Assert.True(snapshot.HostTooOld);
        Assert.Equal(SetupAvailability.NotSupportedByHost, snapshot.Availability);
        Assert.Contains(((int)status).ToString(), snapshot.Problem!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task An_error_page_where_a_report_was_expected_is_reported_with_the_status()
    {
        var snapshot = await Client(StubHandler.Returning(
                HttpStatusCode.InternalServerError, "<html>oops</html>"))
            .ReadSetupAsync(Endpoints);

        Assert.Equal(SetupAvailability.Unreadable, snapshot.Availability);
        Assert.Contains("HTTP 500", snapshot.Problem!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task An_unreachable_gateway_makes_setup_unreachable_not_unset_up()
    {
        var snapshot = await Client(StubHandler.Throwing(new HttpRequestException("refused")))
            .ReadSetupAsync(Endpoints);

        Assert.Equal(SetupAvailability.Unreachable, snapshot.Availability);
        Assert.False(snapshot.NeedsOperator);
        Assert.False(snapshot.IsReady);
    }

    [Fact]
    public async Task Setup_is_read_from_the_documented_path()
    {
        var handler = StubHandler.Returning(HttpStatusCode.OK, """{"state":"ready"}""");
        await Client(handler).ReadSetupAsync(Endpoints);

        Assert.Equal("http://chaos-node:8080/host/setup", handler.Requests[0].RequestUri!.ToString());
    }

    // ----------------------------------------------------------- run setup --

    [Fact]
    public async Task A_setup_run_is_posted_to_the_documented_path()
    {
        var handler = StubHandler.Returning(HttpStatusCode.Accepted, string.Empty);
        var result = await Client(handler).RunSetupAsync(Endpoints);

        Assert.True(result.Accepted);
        Assert.Equal(HttpMethod.Post, handler.Requests[0].Method);
        Assert.Equal("http://chaos-node:8080/host/setup/run", handler.Requests[0].RequestUri!.ToString());
    }

    [Fact]
    public async Task A_host_that_cannot_run_setup_says_so_without_blaming_the_platform()
    {
        var result = await Client(StubHandler.Returning(HttpStatusCode.NotFound, string.Empty))
            .RunSetupAsync(Endpoints);

        Assert.False(result.Accepted);
        Assert.True(result.HostTooOld);
        Assert.Contains("older than this shell", result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_refusal_carries_the_platforms_own_words()
    {
        var result = await Client(StubHandler.Returning(
                HttpStatusCode.Conflict, "Setup is already running."))
            .RunSetupAsync(Endpoints);

        Assert.False(result.Accepted);
        Assert.False(result.HostTooOld);
        Assert.Contains("Setup is already running.", result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_setup_run_that_timed_out_does_not_claim_it_failed()
    {
        var result = await Client(StubHandler.Throwing(new TaskCanceledException()))
            .RunSetupAsync(Endpoints);

        Assert.False(result.Accepted);
        Assert.Contains("may still be running it", result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Every_outcome_carries_a_sentence()
    {
        var outcomes = new[]
        {
            await Client(StubHandler.Returning(HttpStatusCode.OK, string.Empty)).RunSetupAsync(Endpoints),
            await Client(StubHandler.Returning(HttpStatusCode.NotFound, string.Empty)).RunSetupAsync(Endpoints),
            await Client(StubHandler.Returning(HttpStatusCode.BadRequest, string.Empty)).RunSetupAsync(Endpoints),
            await Client(StubHandler.Throwing(new HttpRequestException("refused"))).RunSetupAsync(Endpoints),
        };

        Assert.All(outcomes, o => Assert.False(string.IsNullOrWhiteSpace(o.Message)));
    }
}

/// <summary>Reading the gateway's own health body.</summary>
public sealed class PlatformHealthGatewayFieldsTests
{
    [Theory]
    [InlineData("up", BackendState.Up)]
    [InlineData("down", BackendState.Down)]
    [InlineData("starting", BackendState.Starting)]
    public void The_gateways_backend_word_is_understood(string word, BackendState expected)
    {
        Assert.True(PlatformHealth.TryRead($$"""{"status":"ok","backend":"{{word}}"}""", out var health, out _));
        Assert.Equal(expected, health!.BackendState);
    }

    [Theory]
    [InlineData("wobbling")]
    [InlineData("")]
    public void A_backend_word_we_do_not_know_is_never_read_as_up(string word)
    {
        Assert.True(PlatformHealth.TryRead($$"""{"status":"ok","backend":"{{word}}"}""", out var health, out _));

        Assert.Equal(BackendState.Unknown, health!.BackendState);
        Assert.NotEqual(BackendState.Up, health.BackendState);
    }

    [Fact]
    public void A_body_with_no_backend_field_is_unknown_rather_than_up()
    {
        Assert.True(PlatformHealth.TryRead("""{"status":"ok"}""", out var health, out _));

        Assert.Null(health!.Backend);
        Assert.Equal(BackendState.Unknown, health.BackendState);
    }

    [Fact]
    public void The_gateways_nested_version_is_found_as_well_as_a_flat_one()
    {
        Assert.True(PlatformHealth.TryRead(
            """{"status":"ok","host":{"version":"0.5.0"}}""", out var nested, out _));
        Assert.Equal("0.5.0", nested!.Version);

        Assert.True(PlatformHealth.TryRead("""{"status":"ok","version":"0.4.0"}""", out var flat, out _));
        Assert.Equal("0.4.0", flat!.Version);
    }

    [Fact]
    public void The_flat_version_wins_when_both_are_present()
    {
        Assert.True(PlatformHealth.TryRead(
            """{"status":"ok","version":"0.4.0","host":{"version":"0.5.0"}}""", out var health, out _));

        Assert.Equal("0.4.0", health!.Version);
    }
}
