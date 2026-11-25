import pytest
from dsy_acceptance.clients import api
from dsy_acceptance.e2e import control
from dsy_acceptance.fixtures import bundles


@pytest.mark.scenario("C1")
def test_a_paused_job_releases_its_slot_and_resumes(client: api.Api, batches):
    """C1: pause and resume of a running paced job (`cpu_burn` with a checkpoint).

    Asserts the state sequence pausing, paused, succeeded and that no task holds a slot while the
    job is paused. That the resumed job finishes from its checkpoint is not asserted yet: it waits
    for the checkpoint epic. Lower level proof: the `pause-resume` scenario of
    `dsy_dispatchapi.smoke`, whose `PAUSE_RESUME` is the full sequence.
    """
    source = client.upload_source(bundles.variant("c1"))
    document = api.template(source, control.BURN, checkpoint=True)
    control.pause_resume(client, control.control_call(client), document, batches())
