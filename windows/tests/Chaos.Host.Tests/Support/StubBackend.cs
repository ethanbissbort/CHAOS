using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Tests.Support;

/// <summary>
/// Stands in for the Python/FastAPI backend: a real Kestrel listener on
/// 127.0.0.1 with an ephemeral port, so the gateway's proxy path is exercised
/// over a real socket without the platform needing to be running.
/// </summary>
internal sealed class StubBackend : IAsyncDisposable
{
    private readonly WebApplication _app;

    private StubBackend(WebApplication app, string url, StubBackendSettings settings, ConcurrentQueue<RecordedRequest> requests)
    {
        _app = app;
        Url = url;
        Settings = settings;
        Requests = requests;
    }

    /// <summary>Base URL, for example <c>http://127.0.0.1:41234</c>.</summary>
    public string Url { get; }

    /// <summary>Every request the stub received, in arrival order.</summary>
    public ConcurrentQueue<RecordedRequest> Requests { get; }

    /// <summary>Knobs the tests turn.</summary>
    public StubBackendSettings Settings { get; }

    public static async Task<StubBackend> StartAsync()
    {
        var settings = new StubBackendSettings();
        var requests = new ConcurrentQueue<RecordedRequest>();

        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel(kestrel => kestrel.Listen(IPAddress.Loopback, 0));

        var app = builder.Build();

        app.Use(async (context, next) =>
        {
            requests.Enqueue(RecordedRequest.From(context));
            await next(context);
        });

        app.MapGet("/health", () => Results.Json(new
        {
            status = "ok",
            version = settings.HealthVersion,
            node_role = "primary",
            site_id = "site.site.primary.01",
            physical_control_enabled = false,
        }));

        app.MapGet("/slow", async (HttpContext context) =>
        {
            await Task.Delay(settings.SlowDelay, context.RequestAborted);
            return Results.Text("eventually");
        });

        app.MapMethods(
            "/{**path}",
            ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
            (HttpContext context) => Results.Json(new
            {
                served_by = "stub-python-backend",
                method = context.Request.Method,
                path = context.Request.Path.Value ?? string.Empty,
                query = context.Request.QueryString.Value ?? string.Empty,
                headers = context.Request.Headers.ToDictionary(
                    header => header.Key,
                    header => header.Value.ToString(),
                    StringComparer.OrdinalIgnoreCase),
            }));

        await app.StartAsync();

        var addresses = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()
            ?? throw new InvalidOperationException("Kestrel did not expose its bound addresses.");

        return new StubBackend(app, addresses.Addresses.First(), settings, requests);
    }

    /// <summary>
    /// Reserves and immediately releases a loopback port, so the caller has an
    /// address that refuses connections. Lets the backend-down tests run without
    /// assuming any particular port is free.
    /// </summary>
    /// <returns>A loopback URL nothing is listening on.</returns>
    public static string ReserveClosedLoopbackUrl()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return $"http://127.0.0.1:{port}";
    }

    public async ValueTask DisposeAsync()
    {
        await _app.StopAsync();
        await _app.DisposeAsync();
    }
}

/// <summary>Mutable knobs on the stub backend.</summary>
internal sealed class StubBackendSettings
{
    /// <summary>Version reported on <c>/health</c>. Matches the platform version contract by default.</summary>
    public string HealthVersion { get; set; } = "0.4.0";

    /// <summary>Delay applied to <c>/slow</c>, for timeout tests.</summary>
    public TimeSpan SlowDelay { get; set; } = TimeSpan.FromSeconds(30);
}

/// <summary>A request as the stub backend saw it.</summary>
internal sealed record RecordedRequest(
    string Method,
    string Path,
    string Query,
    IReadOnlyDictionary<string, string> Headers)
{
    public static RecordedRequest From(HttpContext context) => new(
        context.Request.Method,
        context.Request.Path.Value ?? string.Empty,
        context.Request.QueryString.Value ?? string.Empty,
        context.Request.Headers.ToDictionary(
            header => header.Key,
            header => header.Value.ToString(),
            StringComparer.OrdinalIgnoreCase));
}
