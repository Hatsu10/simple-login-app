import enum
import hashlib

import arrow
import bcrypt
import stripe
from arrow import Arrow
from flask_login import UserMixin
from sqlalchemy_utils import ArrowType

from app import s3
from app.config import EMAIL_DOMAIN, MAX_NB_EMAIL_FREE_PLAN, URL
from app.extensions import db
from app.log import LOG
from app.oauth_models import Scope
from app.utils import convert_to_id, random_string, random_words


class ModelMixin(object):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    created_at = db.Column(ArrowType, default=arrow.utcnow, nullable=False)
    updated_at = db.Column(ArrowType, default=None, onupdate=arrow.utcnow)

    _repr_hide = ["created_at", "updated_at"]

    @classmethod
    def query(cls):
        return db.session.query(cls)

    @classmethod
    def get(cls, id):
        return cls.query.get(id)

    @classmethod
    def get_by(cls, **kw):
        return cls.query.filter_by(**kw).first()

    @classmethod
    def filter_by(cls, **kw):
        return cls.query.filter_by(**kw)

    @classmethod
    def get_or_create(cls, **kw):
        r = cls.get_by(**kw)
        if not r:
            r = cls(**kw)
            db.session.add(r)

        return r

    @classmethod
    def create(cls, **kw):
        r = cls(**kw)
        db.session.add(r)
        return r

    def save(self):
        db.session.add(self)

    @classmethod
    def delete(cls, obj_id):
        cls.query.filter(cls.id == obj_id).delete()

    def __repr__(self):
        values = ", ".join(
            "%s=%r" % (n, getattr(self, n))
            for n in self.__table__.c.keys()
            if n not in self._repr_hide
        )
        return "%s(%s)" % (self.__class__.__name__, values)


class File(db.Model, ModelMixin):
    path = db.Column(db.String(128), unique=True, nullable=False)

    def get_url(self):
        return s3.get_url(self.path)


class PlanEnum(enum.Enum):
    free = 0
    trial = 1
    monthly = 2
    yearly = 3


class User(db.Model, ModelMixin, UserMixin):
    __tablename__ = "users"
    email = db.Column(db.String(128), unique=True, nullable=False)
    salt = db.Column(db.String(128), nullable=False)
    password = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)

    activated = db.Column(db.Boolean, default=False, nullable=False)

    plan = db.Column(
        db.Enum(PlanEnum),
        nullable=False,
        default=PlanEnum.free,
        server_default=PlanEnum.free.name,
    )

    # only relevant for trial period
    plan_expiration = db.Column(ArrowType)

    stripe_customer_id = db.Column(db.String(128), unique=True)
    stripe_card_token = db.Column(db.String(128), unique=True)
    stripe_subscription_id = db.Column(db.String(128), unique=True)

    profile_picture_id = db.Column(db.ForeignKey(File.id), nullable=True)
    is_developer = db.Column(db.Boolean, nullable=False, server_default="0")

    # contain the list of promo codes user has used. Promo codes are separated by ","
    promo_codes = db.Column(db.Text, nullable=True)

    profile_picture = db.relationship(File)

    def should_upgrade(self):
        """User is invited to upgrade if they are in free plan or their trial ends soon"""
        if self.plan == PlanEnum.free:
            return True
        elif self.plan == PlanEnum.trial and self.plan_expiration < arrow.now().shift(
            weeks=1
        ):
            return True
        return False

    def is_premium(self):
        return self.plan in (PlanEnum.monthly, PlanEnum.yearly)

    def can_create_new_email(self):
        if self.is_premium():
            return True
        # plan not expired yet
        elif self.plan == PlanEnum.trial and self.plan_expiration > arrow.now():
            return True
        else:  # free or trial expired
            return GenEmail.filter_by(user_id=self.id).count() < MAX_NB_EMAIL_FREE_PLAN

    def set_password(self, password):
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(password.encode(), salt).decode()
        self.salt = salt.decode()
        self.password = password_hash

    def check_password(self, password) -> bool:
        password_hash = bcrypt.hashpw(password.encode(), self.salt.encode())
        return self.password.encode() == password_hash

    def profile_picture_url(self):
        if self.profile_picture_id:
            return self.profile_picture.get_url()
        else:  # use gravatar
            hash_email = hashlib.md5(self.email.encode("utf-8")).hexdigest()
            return f"https://www.gravatar.com/avatar/{hash_email}"

    def plan_current_period_end(self) -> Arrow:
        if not self.stripe_subscription_id:
            LOG.error(
                "plan_current_period_end should not be called with empty stripe_subscription_id"
            )
            return None

        current_period_end_ts = stripe.Subscription.retrieve(
            self.stripe_subscription_id
        )["current_period_end"]

        return arrow.get(current_period_end_ts)

    def get_promo_codes(self) -> [str]:
        if not self.promo_codes:
            return []
        return self.promo_codes.split(",")

    def save_new_promo_code(self, promo_code):
        current_promo_codes = self.get_promo_codes()
        current_promo_codes.append(promo_code)

        self.promo_codes = ",".join(current_promo_codes)


class ActivationCode(db.Model, ModelMixin):
    """For activate user account"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    code = db.Column(db.String(128), unique=True, nullable=False)

    user = db.relationship(User)

    # the activation code is valid for 1h
    expired = db.Column(ArrowType, default=arrow.now().shift(hours=1))


class ResetPasswordCode(db.Model, ModelMixin):
    """For resetting password"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    code = db.Column(db.String(128), unique=True, nullable=False)

    user = db.relationship(User)

    # the activation code is valid for 1h
    expired = db.Column(ArrowType, default=arrow.now().shift(hours=1), nullable=False)


class Partner(db.Model, ModelMixin):
    email = db.Column(db.String(128))
    name = db.Column(db.String(128))
    website = db.Column(db.String(1024))
    additional_information = db.Column(db.Text)

    # If apply from a authenticated user, set user_id to the user who has applied for partnership
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=True)


# <<< OAUTH models >>>


def generate_oauth_client_id(client_name) -> str:
    oauth_client_id = convert_to_id(client_name) + "-" + random_string()

    # check that the client does not exist yet
    if not Client.get_by(oauth_client_id=oauth_client_id):
        LOG.debug("generate oauth_client_id %s", oauth_client_id)
        return oauth_client_id

    # Rerun the function
    LOG.warning(
        "client_id %s already exists, generate a new client_id", oauth_client_id
    )
    return generate_oauth_client_id(client_name)


class Client(db.Model, ModelMixin):
    oauth_client_id = db.Column(db.String(128), unique=True, nullable=False)
    oauth_client_secret = db.Column(db.String(128), nullable=False)

    name = db.Column(db.String(128), nullable=False)
    home_url = db.Column(db.String(1024))
    published = db.Column(db.Boolean, default=False, nullable=False)

    # user who created this client
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    icon_id = db.Column(db.ForeignKey(File.id), nullable=True)

    icon = db.relationship(File)

    def nb_user(self):
        return ClientUser.filter_by(client_id=self.id).count()

    def get_scopes(self) -> [Scope]:
        # todo: client can choose which scopes they want to have access
        return [Scope.NAME, Scope.EMAIL, Scope.AVATAR_URL]

    @classmethod
    def create_new(cls, name, user_id) -> "Client":
        # generate a client-id
        oauth_client_id = generate_oauth_client_id(name)
        oauth_client_secret = random_string(40)
        client = Client.create(
            name=name,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            user_id=user_id,
        )

        return client

    def get_icon_url(self):
        if self.icon_id:
            return self.icon.get_url()
        else:
            return URL + "/static/default-icon.svg"


class RedirectUri(db.Model, ModelMixin):
    """Valid redirect uris for a client"""

    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)
    uri = db.Column(db.String(1024), nullable=False)

    client = db.relationship(Client, backref="redirect_uris")


class AuthorizationCode(db.Model, ModelMixin):
    code = db.Column(db.String(128), unique=True, nullable=False)
    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    scope = db.Column(db.String(128))
    redirect_uri = db.Column(db.String(1024))

    user = db.relationship(User, lazy=False)
    client = db.relationship(Client, lazy=False)


class OauthToken(db.Model, ModelMixin):
    access_token = db.Column(db.String(128), unique=True)
    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    scope = db.Column(db.String(128))
    redirect_uri = db.Column(db.String(1024))

    user = db.relationship(User)
    client = db.relationship(Client)


def generate_email() -> str:
    """generate an email address that does not exist before"""
    random_email = random_words() + "@" + EMAIL_DOMAIN

    # check that the client does not exist yet
    if not GenEmail.get_by(email=random_email):
        LOG.debug("generate email %s", random_email)
        return random_email

    # Rerun the function
    LOG.warning("email %s already exists, generate a new email", random_email)
    return generate_email()


class GenEmail(db.Model, ModelMixin):
    """Generated email"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    email = db.Column(db.String(128), unique=True, nullable=False)

    enabled = db.Column(db.Boolean(), default=True, nullable=False)

    @classmethod
    def create_new_gen_email(cls, user_id):
        random_email = generate_email()
        return GenEmail.create(user_id=user_id, email=random_email)

    def __repr__(self):
        return f"<GenEmail {self.id} {self.email}>"


class ClientUser(db.Model, ModelMixin):
    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_client_user"),
    )

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)

    # Null means client has access to user original email
    gen_email_id = db.Column(
        db.ForeignKey(GenEmail.id, ondelete="cascade"), nullable=True
    )

    gen_email = db.relationship(GenEmail, backref="client_users")

    user = db.relationship(User)
    client = db.relationship(Client)

    def get_email(self):
        return self.gen_email.email if self.gen_email_id else self.user.email

    def get_user_info(self) -> dict:
        """return user info according to client scope
        Return dict with key being scope name. For now all the fields are the same for all clients:

        {
          "client": "Demo",
          "email": "test-avk5l@mail-tester.com",
          "email_verified": true,
          "id": 1,
          "name": "Son GM",
          "avatar_url": "http://s3..."
        }

        """
        res = {"id": self.id, "client": self.client.name, "email_verified": True}

        for scope in self.client.get_scopes():
            if scope == Scope.NAME:
                res[Scope.NAME.value] = self.user.name
            elif scope == Scope.AVATAR_URL:
                if self.user.profile_picture_id:
                    res[Scope.AVATAR_URL.value] = self.user.profile_picture.get_url()
                else:
                    res[Scope.AVATAR_URL.value] = None
            elif scope == Scope.EMAIL:
                # Use generated email
                if self.gen_email_id:
                    LOG.debug(
                        "Use gen email for user %s, client %s", self.user, self.client
                    )
                    res[Scope.EMAIL.value] = self.gen_email.email
                # Use user original email
                else:
                    res[Scope.EMAIL.value] = self.user.email

        return res