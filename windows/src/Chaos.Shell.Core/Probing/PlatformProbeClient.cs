using System.Net;

namespace Chaos.Shell.Core;

/// <summary>What asking the platform to run setup produced.</summary>
/// <param name="Accepted">The platform took the request.</param>
/// <param name="Message">What to show the operator, either way.</param>
/// <param name="HostTooOld">The endpoint is not there — a 404, not a refusal.</param>
public sealed record SetupRunResult(bool Accepted, string Message, bool HostTooOld);

/// <summary>
/// The shell's HTTP conversation with the gateway.
/// </summary>
/// <remarks>
/// <para>
/// This lives in the cross-platform half deliberately. Everything it decides —
/// that a 503 from <c>/health</c> still means a reachable gateway, that a 404
/// from <c>/host/setup</c> means an old host rather than a broken one, that a
/// timeout is reported as a sentence rather than as an exception type — is a
/// judgement about what the operator is told, and every one of those is
/// exercised by tests here rather than being discovered on a homestead node.
/// </para>
/// <para>
/// The <see cref="HttpClient"/> is supplied by the caller so a test can hand it
/// a stub handler, and so the shell can keep one client for the process rather
/// than exhausting sockets from a five-second poll.
/// </para>
/// </remarks>
public sealed class PlatformProbeClient
{
    private readonly HttpClient _http;

    public PlatformProbeClient(HttpClient http)
    {
        ArgumentNullException.ThrowIfNull(http);
        _http = http;
    }

    /// <summary>
    /// Probes <c>/health</c>.
    /// </summary>
    /// <remarks>
    /// A 503 with a readable body is a reachable gateway whose backend is down,
    /// which is a completely different situation from nothing listening — and
    /// the one where an operator most needs to be pointed at the right process.
    /// Any HTTP answer at all counts as reachable; only a transport failure does
    /// not.
    /// </remarks>
    public async Task<GatewayProbe> ProbeGatewayAsync(
        HostEndpoints endpoints,
        GatewayProbe previous,
        DateTimeOffset nowUtc,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(endpoints);
        ArgumentNullException.ThrowIfNull(previous);

        try
        {
            using var response = await _http
                .GetAsync(endpoints.Health, cancellationToken)
                .ConfigureAwait(false);

            var body = await response.Content
                .ReadAsStringAsync(cancellationToken)
                .ConfigureAwait(false);

            PlatformHealth.TryRead(body, out var health, out _);

            return GatewayProbe.Answered(health, (int)response.StatusCode, nowUtc);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            // A cancelled-looking exception that is NOT our cancellation is a
            // timeout, and saying "the request was cancelled" about a control
            // system's readiness probe tells an operator nothing.
            var message = ex is OperationCanceledException
                ? $"no answer within {Describe(_http.Timeout)}"
                : Flatten(ex);

            return GatewayProbe.Silent(message, previous.ConsecutiveFailures + 1, previous.LastSuccessUtc);
        }
    }

    /// <summary>
    /// Reads <c>GET /host/setup</c>.
    /// </summary>
    /// <remarks>
    /// A 404 means this gateway predates the endpoint. That is reported as a
    /// missing capability, never as a fault: telling an operator their platform
    /// is broken because their shell is newer than it would be a lie.
    /// </remarks>
    public async Task<SetupSnapshot> ReadSetupAsync(
        HostEndpoints endpoints,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        try
        {
            using var response = await _http
                .GetAsync(endpoints.Setup, cancellationToken)
                .ConfigureAwait(false);

            if (response.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.MethodNotAllowed
                or HttpStatusCode.NotImplemented)
            {
                return SetupSnapshot.Absent($"HTTP {(int)response.StatusCode} from {endpoints.Setup}");
            }

            var body = await response.Content
                .ReadAsStringAsync(cancellationToken)
                .ConfigureAwait(false);

            // 200 and 503 both carry the report: the endpoint answers 503 when
            // setup has not completed, which is exactly the state we came for.
            var snapshot = SetupSnapshotReader.Read(body);

            if (snapshot.Availability == SetupAvailability.Unreadable
                && !response.IsSuccessStatusCode)
            {
                return SetupSnapshot.Unreadable(
                    $"The gateway answered HTTP {(int)response.StatusCode} for {endpoints.Setup} and the "
                    + $"body was not a setup report. {snapshot.Problem}");
            }

            return snapshot;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            var message = ex is OperationCanceledException
                ? $"The gateway did not answer {endpoints.Setup} within {Describe(_http.Timeout)}."
                : $"The shell could not ask {endpoints.Setup}: {Flatten(ex)}";

            return SetupSnapshot.Unreachable(message);
        }
    }

    /// <summary>Asks the platform to run or retry setup via <c>POST /host/setup/run</c>.</summary>
    public async Task<SetupRunResult> RunSetupAsync(
        HostEndpoints endpoints,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        try
        {
            using var content = new StringContent(string.Empty);
            using var response = await _http
                .PostAsync(endpoints.RunSetup, content, cancellationToken)
                .ConfigureAwait(false);

            if (response.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.MethodNotAllowed
                or HttpStatusCode.NotImplemented)
            {
                return new SetupRunResult(
                    Accepted: false,
                    $"This gateway does not offer {endpoints.RunSetup} (HTTP "
                    + $"{(int)response.StatusCode}), so the shell cannot run setup for you. It is "
                    + "older than this shell expects.",
                    HostTooOld: true);
            }

            if (response.IsSuccessStatusCode)
            {
                return new SetupRunResult(
                    Accepted: true,
                    "The platform accepted the setup request. Progress is shown above as it reports it.",
                    HostTooOld: false);
            }

            var body = await response.Content
                .ReadAsStringAsync(cancellationToken)
                .ConfigureAwait(false);

            return new SetupRunResult(
                Accepted: false,
                $"The platform refused the setup request with HTTP {(int)response.StatusCode}"
                + (string.IsNullOrWhiteSpace(body) ? "." : $": {Trim(body)}"),
                HostTooOld: false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            var message = ex is OperationCanceledException
                ? $"The platform did not answer the setup request within {Describe(_http.Timeout)}. "
                  + "It may still be running it — check the setup state above before asking again."
                : $"The shell could not send the setup request: {Flatten(ex)}";

            return new SetupRunResult(Accepted: false, message, HostTooOld: false);
        }
    }

    /// <summary>
    /// The innermost message of an exception chain. <c>HttpRequestException</c>
    /// wraps the socket error that actually says "connection refused", and that
    /// inner sentence is the one an operator can act on.
    /// </summary>
    internal static string Flatten(Exception exception)
    {
        var current = exception;
        while (current.InnerException is not null)
        {
            current = current.InnerException;
        }

        var message = current.Message.Trim();
        return message.Length == 0 ? exception.GetType().Name : message;
    }

    private static string Describe(TimeSpan timeout) => RelativeTime.Describe(timeout);

    /// <summary>Keeps an error body short enough to sit in a status line.</summary>
    private static string Trim(string body)
    {
        var text = body.Trim();
        return text.Length <= 300 ? text : string.Concat(text.AsSpan(0, 299), "…");
    }
}
