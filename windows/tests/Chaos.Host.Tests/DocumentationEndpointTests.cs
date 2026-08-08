using System.Net;
using System.Text.Json;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// What the gateway says about the documentation listener on
/// <c>GET /host/info</c> — the one place the shell and the operator console
/// find out where the manuals are.
/// </summary>
/// <remarks>
/// The listener itself never binds during these tests: the gateway is hosted on
/// an in-memory test server, and a documentation port is a LAN port. What is
/// under test here is the report, which is derived from configuration and from
/// where the site was resolved, not from a live socket — precisely so that it
/// can be answered on a machine where the listener failed to start.
/// </remarks>
public sealed class DocumentationEndpointTests
{
    [Fact]
    public async Task Host_info_reports_where_the_documentation_is()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:RootPath", fixture.Root)
            .Build();
        using var client = factory.CreateClient();

        var documentation = await GetDocumentationAsync(client);

        Assert.True(documentation.GetProperty("enabled").GetBoolean());
        Assert.True(documentation.GetProperty("generated").GetBoolean());
        Assert.Equal(fixture.Root, documentation.GetStringProperty("path"));

        // The default is a separate port, not a path on the gateway.
        Assert.Equal("http://0.0.0.0:8090", documentation.GetStringProperty("listenUrl"));
        Assert.False(documentation.GetProperty("servedHere").GetBoolean());
    }

    [Fact]
    public async Task Host_info_reports_a_url_a_browser_can_actually_open()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:RootPath", fixture.Root)
            .Build();
        using var client = factory.CreateClient();

        // Asked for through the address a phone on the LAN used, the answer
        // names that address - not the 0.0.0.0 the listener is bound to, which
        // is not a link anyone can follow.
        var documentation = await GetDocumentationAsync(client, "http://192.168.1.10:8080/host/info");

        Assert.Equal("http://192.168.1.10:8090/", documentation.GetStringProperty("url"));
    }

    [Fact]
    public async Task The_documentation_port_is_configurable()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:RootPath", fixture.Root)
            .With("Docs:ListenUrl", "http://0.0.0.0:9443")
            .Build();
        using var client = factory.CreateClient();

        var documentation = await GetDocumentationAsync(client, "http://192.168.1.10:8080/host/info");

        Assert.Equal("http://0.0.0.0:9443", documentation.GetStringProperty("listenUrl"));
        Assert.Equal("http://192.168.1.10:9443/", documentation.GetStringProperty("url"));
    }

    [Fact]
    public async Task A_loopback_documentation_binding_is_reported_as_it_is()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:RootPath", fixture.Root)
            .With("Docs:ListenUrl", "http://127.0.0.1:8090")
            .Build();
        using var client = factory.CreateClient();

        // Not a wildcard, so nothing is substituted: an operator who bound the
        // manual to loopback is told the manual is on loopback.
        var documentation = await GetDocumentationAsync(client, "http://192.168.1.10:8080/host/info");

        Assert.Equal("http://127.0.0.1:8090/", documentation.GetStringProperty("url"));
    }

    [Fact]
    public async Task Host_info_says_when_the_documentation_has_not_been_generated()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:RootPath", Path.Combine(Path.GetTempPath(), "chaos-docs-that-do-not-exist"))
            .Build();
        using var client = factory.CreateClient();

        var documentation = await GetDocumentationAsync(client);

        Assert.True(documentation.GetProperty("enabled").GetBoolean());
        Assert.False(documentation.GetProperty("generated").GetBoolean());
        Assert.Equal(JsonValueKind.Null, documentation.GetProperty("path").ValueKind);
        Assert.NotEmpty(documentation.GetProperty("searched").EnumerateArray());
    }

    [Fact]
    public async Task The_documentation_listener_can_be_switched_off()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:Enabled", "false")
            .Build();
        using var client = factory.CreateClient();

        var documentation = await GetDocumentationAsync(client);

        Assert.False(documentation.GetProperty("enabled").GetBoolean());

        // Nothing to link to, and the gateway says so rather than naming a URL
        // that refuses connections.
        Assert.Equal(JsonValueKind.Null, documentation.GetProperty("url").ValueKind);
    }

    [Fact]
    public async Task Switching_the_documentation_off_leaves_the_gateway_alone()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("Docs:Enabled", "false")
            .Build();
        using var client = factory.CreateClient();

        using var health = await client.GetAsync("/health/live");
        Assert.Equal(HttpStatusCode.OK, health.StatusCode);

        using var proxied = await client.GetAsync("/api/v1/alarms/active");
        Assert.Equal("python", proxied.Headers.GetValues("X-Chaos-Route-Owner").Single());
    }

    [Fact]
    public void The_host_refuses_to_share_one_port_between_the_gateway_and_the_documentation()
    {
        using var factory = ChaosHostFactory.For()
            .With("ListenUrl", "http://0.0.0.0:8080")
            .With("Docs:ListenUrl", "http://0.0.0.0:8080")
            .Build();

        var thrown = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(thrown);
        Assert.Contains("Chaos:Docs:ListenUrl", thrown!.ToString(), StringComparison.Ordinal);
        Assert.Contains("SEPARATE port", thrown.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void The_host_refuses_a_documentation_url_that_is_not_a_url()
    {
        using var factory = ChaosHostFactory.For()
            .With("Docs:ListenUrl", "8090")
            .Build();

        var thrown = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(thrown);
        Assert.Contains("Chaos:Docs:ListenUrl", thrown!.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Host_routes_says_the_documentation_is_not_on_this_listener()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        var body = await (await client.GetAsync("/host/routes")).ReadJsonAsync();
        var unowned = body.GetProperty("notOwnedByTheManifest").EnumerateArray()
            .Select(element => element.GetString() ?? string.Empty)
            .ToList();

        Assert.Contains(unowned, entry => entry.Contains("documentation", StringComparison.OrdinalIgnoreCase)
                                       && entry.Contains("own port", StringComparison.OrdinalIgnoreCase));
    }

    private static async Task<JsonElement> GetDocumentationAsync(HttpClient client, string url = "/host/info")
    {
        using var response = await client.GetAsync(url);
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var body = await response.ReadJsonAsync();
        return body.GetProperty("documentation");
    }
}
