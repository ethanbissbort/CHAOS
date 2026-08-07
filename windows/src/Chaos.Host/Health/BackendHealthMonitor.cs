using System.Net.Http.Json;
using System.Text.Json;
using Chaos.Host.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Health;

/// <summary>
/// Polls the Python backend's health endpoint and keeps
/// <see cref="BackendHealthState"/> current.
/// </summary>
/// <remarks>
/// <para>
/// Backoff is exponential from one second to
/// <see cref="ChaosHostOptions.BackendHealthMaxBackoff"/> while the backend is
/// unreachable, and returns to <see cref="ChaosHostOptions.BackendHealthInterval"/>
/// on recovery.
/// </para>
/// <para>
/// Logging is deliberately quiet. A transition (up-&gt;down, down-&gt;up) logs once
/// at Warning or Information. While the backend stays down, the repeat warning
/// is emitted at most once per
/// <see cref="ChaosHostOptions.BackendHealthLogQuietPeriod"/> and everything
/// else goes to Debug. During a multi-hour outage the log has to remain
/// readable, because that log is the record of the outage.
/// </para>
/// </remarks>
internal sealed partial class BackendHealthMonitor : BackgroundService
{
    /// <summary>Name of the named <see cref="HttpClient"/> used for polling.</summary>
    public const string HttpClientName = "chaos-backend-health";

    private readonly IHttpClientFactory _clientFactory;
    private readonly BackendHealthState _state;
    private readonly ChaosHostOptions _options;
    private readonly ILogger<BackendHealthMonitor> _logger;
    private readonly Uri _healthUri;
    private readonly DateTimeOffset _startDeadlineUtc;

    private DateTimeOffset _lastFailureLogUtc = DateTimeOffset.MinValue;

    public BackendHealthMonitor(
        IHttpClientFactory clientFactory,
        BackendHealthState state,
        IOptions<ChaosHostOptions> options,
        ILogger<BackendHealthMonitor> logger)
    {
        _clientFactory = clientFactory;
        _state = state;
        _options = options.Value;
        _logger = logger;
        _healthUri = new Uri(_options.BackendUri(), _options.BackendHealthPath);
        _startDeadlineUtc = DateTimeOffset.UtcNow + _options.BackendStartTimeout;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        LogPollingStarted(_healthUri);

        while (!stoppingToken.IsCancellationRequested)
        {
            var succeeded = await PollOnceAsync(stoppingToken).ConfigureAwait(false);

            var delay = succeeded
                ? _options.BackendHealthInterval
                : NextBackoff(_state.Current.ConsecutiveFailures);

            try
            {
                await Task.Delay(delay, stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    /// <summary>
    /// Runs a single poll and updates <see cref="BackendHealthState"/>.
    /// </summary>
    /// <param name="cancellationToken">Cancellation.</param>
    /// <returns>Whether the backend answered successfully.</returns>
    internal async Task<bool> PollOnceAsync(CancellationToken cancellationToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(_options.BackendHealthTimeout);

        try
        {
            var client = _clientFactory.CreateClient(HttpClientName);
            using var response = await client.GetAsync(_healthUri, timeout.Token).ConfigureAwait(false);

            if (!response.IsSuccessStatusCode)
            {
                RecordFailure(
                    $"Backend health endpoint returned HTTP {(int)response.StatusCode} {response.ReasonPhrase}.",
                    (int)response.StatusCode);
                return false;
            }

            var (version, compatible) = await ReadVersionAsync(response, timeout.Token).ConfigureAwait(false);
            if (_state.RecordSuccess((int)response.StatusCode, version, compatible))
            {
                LogBackendReachable(_healthUri, version ?? "unknown");
                _lastFailureLogUtc = DateTimeOffset.MinValue;
            }

            if (compatible == false)
            {
                LogVersionMismatch(version ?? "unknown", HostVersion.PlatformVersionContract);
            }

            return true;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return false;
        }
        catch (OperationCanceledException)
        {
            RecordFailure($"Backend health probe timed out after {_options.BackendHealthTimeout}.", statusCode: null);
            return false;
        }
        catch (HttpRequestException ex)
        {
            RecordFailure(Describe(ex), statusCode: null);
            return false;
        }
        catch (Exception ex) when (ex is not OutOfMemoryException and not StackOverflowException)
        {
            RecordFailure($"{ex.GetType().Name}: {ex.Message}", statusCode: null);
            return false;
        }
    }

    private void RecordFailure(string error, int? statusCode)
    {
        var stillStarting = DateTimeOffset.UtcNow < _startDeadlineUtc && _state.Current.LastSuccessUtc is null;
        var transitioned = _state.RecordFailure(error, statusCode, stillStarting);
        var snapshot = _state.Current;

        var now = DateTimeOffset.UtcNow;
        var quietPeriodElapsed = now - _lastFailureLogUtc >= _options.BackendHealthLogQuietPeriod;

        if (transitioned || quietPeriodElapsed)
        {
            _lastFailureLogUtc = now;
            LogBackendUnreachable(_healthUri, snapshot.BackendWireValue, snapshot.ConsecutiveFailures, error);
        }
        else
        {
            LogBackendStillUnreachable(snapshot.ConsecutiveFailures, error);
        }
    }

    private TimeSpan NextBackoff(int consecutiveFailures)
    {
        if (consecutiveFailures <= 0)
        {
            return _options.BackendHealthInterval;
        }

        // 1s, 2s, 4s, 8s ... capped. Doubling stops at 2^20 so the shift cannot overflow.
        var exponent = Math.Min(consecutiveFailures - 1, 20);
        var seconds = Math.Pow(2, exponent);
        var candidate = TimeSpan.FromSeconds(seconds);
        return candidate > _options.BackendHealthMaxBackoff ? _options.BackendHealthMaxBackoff : candidate;
    }

    private static async Task<(string? Version, bool? Compatible)> ReadVersionAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        try
        {
            var payload = await response.Content
                .ReadFromJsonAsync<JsonElement>(cancellationToken)
                .ConfigureAwait(false);

            if (payload.ValueKind == JsonValueKind.Object
                && payload.TryGetProperty("version", out var versionElement)
                && versionElement.ValueKind == JsonValueKind.String)
            {
                var version = versionElement.GetString();
                return (version, HostVersion.IsPlatformVersionCompatible(version));
            }
        }
        catch (Exception ex) when (ex is JsonException or NotSupportedException or HttpRequestException or OperationCanceledException)
        {
            // A backend that answers but does not report a version is reachable.
            // We say the version is unknown rather than assuming it matches.
        }

        return (null, null);
    }

    private static string Describe(HttpRequestException exception)
    {
        var inner = exception.InnerException;
        return inner is null
            ? exception.Message
            : $"{exception.Message} ({inner.GetType().Name}: {inner.Message})";
    }

    [LoggerMessage(EventId = 1001, Level = LogLevel.Information,
        Message = "Backend health polling started against {HealthUri}.")]
    private partial void LogPollingStarted(Uri healthUri);

    [LoggerMessage(EventId = 1002, Level = LogLevel.Information,
        Message = "Backend is reachable at {HealthUri} (platform version {BackendVersion}).")]
    private partial void LogBackendReachable(Uri healthUri, string backendVersion);

    [LoggerMessage(EventId = 1003, Level = LogLevel.Warning,
        Message = "Backend is {Reachability} at {HealthUri} after {ConsecutiveFailures} consecutive failed health checks: {Error}")]
    private partial void LogBackendUnreachable(Uri healthUri, string reachability, int consecutiveFailures, string error);

    [LoggerMessage(EventId = 1004, Level = LogLevel.Debug,
        Message = "Backend still unreachable ({ConsecutiveFailures} consecutive failures): {Error}")]
    private partial void LogBackendStillUnreachable(int consecutiveFailures, string error);

    [LoggerMessage(EventId = 1005, Level = LogLevel.Warning,
        Message = "Backend reports platform version {BackendVersion}, which does not satisfy this host's contract {ContractVersion}. "
                + "The route-ownership manifest was written against the contract version; verify the API surface before trusting it.")]
    private partial void LogVersionMismatch(string backendVersion, string contractVersion);
}
