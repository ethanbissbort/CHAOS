using Chaos.Host;

// -----------------------------------------------------------------------------
// Project CHAOS - .NET gateway host.
//
// The front door for the platform: the only listener on the LAN, a reverse
// proxy to the Python backend on loopback, the host of the operator console,
// and the place ported subsystems land as the migration proceeds.
//
// Everything is assembled in ChaosHostExtensions so the same host can be built
// from a test or from another entry point without the registration order
// drifting - and the order matters here: the route-ownership manifest is
// validated before anything else starts.
// -----------------------------------------------------------------------------

var builder = WebApplication.CreateBuilder(args);

builder.AddChaosHost();

var app = builder.Build();

app.UseChaosHost();

app.Run();

/// <summary>
/// Entry point. Declared partial and public so
/// <c>WebApplicationFactory&lt;Program&gt;</c> can host the real pipeline in tests
/// rather than a reconstruction of it.
/// </summary>
public partial class Program;
