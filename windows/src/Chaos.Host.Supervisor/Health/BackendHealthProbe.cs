using System.Globalization;
using System.Net;
using System.Text.Json;

namespace Chaos.Host.Supervisor.Health;

/// <summary>What one <c>GET /health</c> told us.</summary>
/// <param name="IsHealthy">
/// True only when the platform itself said <c>"status": "ok"</c>. A socket that
/// accepts a connection is not health.
/// </param>
/// <param name="Detail">Why, in words an operator can act on.</param>
/// <param name="Version">Platform version from the body, when it gave one.</param>
/// <param name="NodeRole">Node role from the body, when it gave one.</param>
public readonly record struct BackendHealthResult(
    bool IsHealthy,
    string Detail,
    string? Version = null,
    string? NodeRole = null);

/// <summary>Readiness and liveness, asked of the backend rather than assumed.</summary>
public interface IBackendHealthProbe
{
    Task<BackendHealthResult> CheckAsync(Uri endpoint, TimeSpan timeout, CancellationToken cancellationToken);
}

/// <summary>
/// Polls the platform's <c>/health</c> endpoint
/// (<c>src/homestead_twin/api/app.py</c>), which answers:
/// <c>{"status":"ok","version":…,"node_role":…,"site_id":…,"physical_control_enabled":…}</c>.
/// </summary>
/// <remarks>
/// Nothing short of the platform's own <c>"ok"</c> counts. A 200 with a body we
/// do not recognise means something is listening on that loopback port and it
/// is not demonstrably our backend — which is a fact worth reporting, not
/// rounding up to healthy.
/// </remarks>
public sealed class HttpBackendHealthProbe : IBackendHealthProbe, IDisposable
{
    private readonly HttpClient _client;
    private readonly bool _ownsClient;
    private bool _disposed;

    public HttpBackendHealthProbe()
    {
        _client = new HttpClient(new SocketsHttpHandler
        {
            ConnectTimeout = TimeSpan.FromSeconds(5),
            PooledConnectionLifetime = TimeSpan.FromMinutes(5),
            AllowAutoRedirect = false,
        })
        {
            // Cancellation is per call, from the caller's timeout.
            Timeout = System.Threading.Timeout.InfiniteTimeSpan,
        };
        _ownsClient = true;
    }

    public HttpBackendHealthProbe(HttpClient client)
    {
        _client = client ?? throw new ArgumentNullException(nameof(client));
        _ownsClient = false;
    }

    public async Task<BackendHealthResult> CheckAsync(Uri endpoint, TimeSpan timeout, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(endpoint);

        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        if (timeout > TimeSpan.Zero)
        {
            timeoutSource.CancelAfter(timeout);
        }

        try
        {
            using var response = await _client
                .GetAsync(endpoint, HttpCompletionOption.ResponseContentRead, timeoutSource.Token)
                .ConfigureAwait(false);

            var body = await response.Content.ReadAsStringAsync(timeoutSource.Token).ConfigureAwait(false);

            if (response.StatusCode != HttpStatusCode.OK)
            {
                return new BackendHealthResult(
                    false,
                    $"{endpoint} answered HTTP {((int)response.StatusCode).ToString(CultureInfo.InvariantCulture)} " +
                    $"{response.ReasonPhrase}");
            }

            return Interpret(endpoint, body);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException)
        {
            return new BackendHealthResult(
                false,
                $"{endpoint} did not answer within {timeout.TotalSeconds.ToString("0.#", CultureInfo.InvariantCulture)}s");
        }
        catch (HttpRequestException ex)
        {
            return new BackendHealthResult(false, $"{endpoint} unreachable: {ex.Message}");
        }
    }

    internal static BackendHealthResult Interpret(Uri endpoint, string body)
    {
        if (string.IsNullOrWhiteSpace(body))
        {
            return new BackendHealthResult(false, $"{endpoint} answered HTTP 200 with an empty body");
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(body);
        }
        catch (JsonException ex)
        {
            return new BackendHealthResult(
                false,
                $"{endpoint} answered HTTP 200 but the body is not JSON ({ex.Message}). " +
                "Something is listening on that port; it has not identified itself as the platform.");
        }

        using (document)
        {
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                return new BackendHealthResult(false, $"{endpoint} answered HTTP 200 with a non-object body");
            }

            var status = ReadString(document.RootElement, "status");
            var version = ReadString(document.RootElement, "version");
            var nodeRole = ReadString(document.RootElement, "node_role");

            if (status is null)
            {
                return new BackendHealthResult(
                    false,
                    $"{endpoint} answered HTTP 200 but the body has no 'status' field, so the platform " +
                    "has not reported itself healthy",
                    version,
                    nodeRole);
            }

            if (!string.Equals(status, "ok", StringComparison.OrdinalIgnoreCase))
            {
                return new BackendHealthResult(
                    false,
                    $"{endpoint} reported status '{status}'",
                    version,
                    nodeRole);
            }

            var detail = $"{endpoint} reported ok";
            if (version is not null)
            {
                detail += $" (platform {version}";
                detail += nodeRole is not null ? $", node role {nodeRole})" : ")";
            }

            return new BackendHealthResult(true, detail, version, nodeRole);
        }
    }

    private static string? ReadString(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (_ownsClient)
        {
            _client.Dispose();
        }
    }
}
