from imbue.minds.desktop_client.api_models import SharingGrantList
from imbue.minds.desktop_client.api_models import SharingGrantsDocument
from imbue.minds.desktop_client.share_grant_validation import PUBLIC_EMAIL_DOMAINS
from imbue.minds.desktop_client.share_grant_validation import normalized_grants_document
from imbue.minds.desktop_client.share_grant_validation import validate_grants_document

# The workspace holds no grants yet, so every entry under test is a new one.
_NOTHING_STORED = SharingGrantsDocument()


def test_validate_grants_document_refuses_each_bad_entry_exactly_once() -> None:
    document = SharingGrantsDocument(
        workspace=SharingGrantList(
            emails=("not-an-address", "friend@example.com", "friend@example.com", "owner@example.com"),
            email_domains=("not a domain", "gmail.com", "partner.org"),
        )
    )

    refusals = validate_grants_document(document, "owner@example.com", _NOTHING_STORED)

    assert [(refusal.value, refusal.message) for refusal in refusals] == [
        ("not-an-address", "not-an-address is not an email address."),
        ("friend@example.com", "friend@example.com is already granted here."),
        ("owner@example.com", "owner@example.com is your own address."),
        ("not a domain", "not a domain is not a domain."),
        ("gmail.com", "gmail.com cannot be granted permissions because it is a public email provider."),
    ]


def test_validate_grants_document_accepts_a_document_that_grants_nobody() -> None:
    assert validate_grants_document(SharingGrantsDocument(), "owner@example.com", _NOTHING_STORED) == ()


def test_validate_grants_document_names_the_scope_and_kind_of_every_refusal() -> None:
    document = SharingGrantsDocument(
        workspace=SharingGrantList(emails=("nope",)),
        services={"notes": SharingGrantList(email_domains=("yahoo.com",))},
    )

    refusals = validate_grants_document(document, "owner@example.com", _NOTHING_STORED)

    assert [(refusal.scope, refusal.kind) for refusal in refusals] == [
        ("workspace", "email"),
        ("notes", "email_domain"),
    ]


def test_validate_grants_document_compares_case_insensitively_after_trimming() -> None:
    # A refusal names the normalized entry and carries the raw string the
    # granter typed, so the panel can mark that row.
    document = SharingGrantsDocument(
        workspace=SharingGrantList(
            emails=("friend@example.com", " Friend@Example.com ", "  OWNER@example.com"),
            email_domains=("partner.org", "PARTNER.ORG", " GMail.com "),
        )
    )

    refusals = validate_grants_document(document, "Owner@Example.com", _NOTHING_STORED)

    assert [(refusal.value, refusal.message) for refusal in refusals] == [
        (" Friend@Example.com ", "friend@example.com is already granted here."),
        ("  OWNER@example.com", "owner@example.com is your own address."),
        ("PARTNER.ORG", "partner.org is already granted here."),
        (" GMail.com ", "gmail.com cannot be granted permissions because it is a public email provider."),
    ]


def test_validate_grants_document_allows_an_individual_address_at_a_public_provider() -> None:
    # A public provider only rules out the whole-domain grant: one person's
    # address there is an ordinary grant.
    document = SharingGrantsDocument(workspace=SharingGrantList(emails=("someone@gmail.com",)))

    assert validate_grants_document(document, "owner@example.com", _NOTHING_STORED) == ()


def test_validate_grants_document_refuses_a_user_id_that_is_not_one() -> None:
    # A user id is opaque, so only emptiness and duplication can be judged.
    document = SharingGrantsDocument(workspace=SharingGrantList(users=("user-1", " user-1 ", "", "   ")))

    refusals = validate_grants_document(document, "owner@example.com", _NOTHING_STORED)

    assert [(refusal.kind, refusal.value, refusal.message) for refusal in refusals] == [
        ("user", " user-1 ", "user-1 is already granted here."),
        ("user", "", " is not a user id."),
        ("user", "   ", " is not a user id."),
    ]


def test_validate_grants_document_grandfathers_a_stored_user_id() -> None:
    stored = SharingGrantsDocument(workspace=SharingGrantList(users=("user-1",)))
    document = SharingGrantsDocument(workspace=SharingGrantList(users=("user-1", "user-2")))

    assert validate_grants_document(document, "owner@example.com", stored) == ()


def test_validate_grants_document_refuses_malformed_addresses_and_domains() -> None:
    document = SharingGrantsDocument(
        workspace=SharingGrantList(
            emails=("a@b@example.com", "no-domain@", "@example.com", "spaced out@example.com", "a@localhost"),
            email_domains=("example", "-bad.org", "bad-.org", "example.c", "under_score.org"),
        )
    )

    refusals = validate_grants_document(document, "owner@example.com", _NOTHING_STORED)

    assert [refusal.value for refusal in refusals] == [
        "a@b@example.com",
        "no-domain@",
        "@example.com",
        "spaced out@example.com",
        "a@localhost",
        "example",
        "-bad.org",
        "bad-.org",
        "example.c",
        "under_score.org",
    ]


def test_validate_grants_document_accepts_ordinary_addresses_and_domains() -> None:
    document = SharingGrantsDocument(
        workspace=SharingGrantList(
            emails=("first.last+tag@sub.example.co.uk", "a@b.io"),
            email_domains=("sub.example.co.uk", "my-company.dev"),
        )
    )

    assert validate_grants_document(document, "owner@example.com", _NOTHING_STORED) == ()


def test_public_email_domains_covers_the_providers_a_domain_grant_must_never_admit() -> None:
    for domain in ("gmail.com", "yahoo.co.uk", "hotmail.com", "outlook.com", "icloud.com", "proton.me", "qq.com"):
        assert domain in PUBLIC_EMAIL_DOMAINS
    assert "example.com" not in PUBLIC_EMAIL_DOMAINS


def test_validate_grants_document_grandfathers_every_entry_the_document_already_carries() -> None:
    # A stored entry is already in force: re-sending it in a whole-document
    # save must never refuse the save, or the owner row the create form seeds
    # (and anything an older panel wrote) would block every later edit.
    stored = SharingGrantsDocument(
        workspace=SharingGrantList(emails=("owner@example.com", "not-an-address"), email_domains=("gmail.com",))
    )
    document = SharingGrantsDocument(
        workspace=SharingGrantList(
            emails=("Owner@Example.com ", "not-an-address", "friend@example.com"),
            email_domains=("gmail.com", "partner.org"),
        )
    )

    assert validate_grants_document(document, "owner@example.com", stored) == ()


def test_validate_grants_document_refuses_a_new_duplicate_of_a_grandfathered_entry() -> None:
    stored = SharingGrantsDocument(workspace=SharingGrantList(emails=("owner@example.com",)))
    document = SharingGrantsDocument(workspace=SharingGrantList(emails=("owner@example.com", "OWNER@example.com")))

    refusals = validate_grants_document(document, "owner@example.com", stored)

    assert [(refusal.value, refusal.message) for refusal in refusals] == [
        ("OWNER@example.com", "owner@example.com is already granted here.")
    ]


def test_validate_grants_document_grandfathers_an_entry_only_in_the_list_that_holds_it() -> None:
    # Widening a grant from one app to the whole workspace is a new grant, so
    # it faces the rules even though the same value sits in the app's list.
    stored = SharingGrantsDocument(services={"notes": SharingGrantList(email_domains=("gmail.com",))})
    document = SharingGrantsDocument(
        workspace=SharingGrantList(email_domains=("gmail.com",)),
        services={"notes": SharingGrantList(email_domains=("gmail.com",))},
    )

    refusals = validate_grants_document(document, "owner@example.com", stored)

    assert [(refusal.scope, refusal.value) for refusal in refusals] == [("workspace", "gmail.com")]


def test_validate_grants_document_reads_a_domain_written_with_a_leading_at_sign() -> None:
    # "@example.com" is how a granter often writes a domain; it is the same
    # grant as "example.com", so a copy of one is a duplicate of the other and
    # a public provider is refused either way.
    document = SharingGrantsDocument(
        workspace=SharingGrantList(email_domains=("@partner.org", "partner.org", "@gmail.com"))
    )

    refusals = validate_grants_document(document, "owner@example.com", _NOTHING_STORED)

    assert [(refusal.value, refusal.message) for refusal in refusals] == [
        ("partner.org", "partner.org is already granted here."),
        ("@gmail.com", "gmail.com cannot be granted permissions because it is a public email provider."),
    ]


def test_validate_grants_document_accepts_internationalized_labels() -> None:
    document = SharingGrantsDocument(
        workspace=SharingGrantList(emails=("私@例え.テスト",), email_domains=("Müller-KG.example",))
    )

    assert validate_grants_document(document, "owner@example.com", _NOTHING_STORED) == ()


def test_normalized_grants_document_stores_what_was_checked() -> None:
    # The file must hold the form the rules compared and the gateway compares. A
    # user id is opaque, so it is only trimmed: lowercasing it would name nobody.
    document = SharingGrantsDocument(
        workspace=SharingGrantList(
            users=(" user-1 ",), emails=(" Friend@Example.COM ",), email_domains=("@Partner.ORG", "MÜLLER-kg.example")
        ),
        services={"notes": SharingGrantList(emails=("Carol@Example.com",))},
    )

    normalized = normalized_grants_document(document)

    assert normalized.workspace.emails == ("friend@example.com",)
    assert normalized.workspace.email_domains == ("partner.org", "müller-kg.example")
    assert normalized.workspace.users == ("user-1",)
    assert normalized.services["notes"].emails == ("carol@example.com",)
