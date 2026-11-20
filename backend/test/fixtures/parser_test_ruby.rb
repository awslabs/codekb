# frozen_string_literal: true
#
# Ruby parser fixture. Exercises the constructs RubyParser handles:
# modules, classes (plain, inherited, and qualified), singleton methods,
# `class << self`, attribute macros, aliases, sticky and inline visibility,
# every parameter form, constants, class variables, and the call forms the
# flow analyzer needs to resolve.

MAX_RETRIES = 3
DEFAULT_CURRENCY = "USD"

def normalize_amount(amount)
  amount.round(2)
end

module Billing
  TAX_RATE = 0.08

  class AuditLog
    def record(action)
      persist(action)
    end

    def persist(action)
      action
    end
  end

  class PaymentGateway
    attr_reader :currency

    def initialize(currency = DEFAULT_CURRENCY)
      @currency = currency
    end

    def charge(amount)
      authorize(amount)
    end

    def authorize(amount)
      amount
    end
  end

  class InvoiceService < BaseService
    include Auditable
    has_many :line_items, dependent: :destroy
    validates :total, presence: true

    @@instances = 0

    LIMIT = 100

    attr_accessor :currency
    attr_writer :clock
    attr_reader :gateway, :audit

    def initialize(currency: DEFAULT_CURRENCY, **options)
      @currency = currency
      @options = options
      @gateway = PaymentGateway.new(currency)
      @audit = AuditLog.new
    end

    # Exercises: bare call with args, two instance-variable calls resolved via
    # the constructor's `@ivar = Foo.new`, and a paren-less zero-argument call.
    def create_invoice(items, notify: true)
      amount = calculate_total(items)
      @gateway.charge(amount)
      @audit.record("created")
      finalize
      amount
    end

    def calculate_total(items, *extras, &block)
      items.length
    end

    # Exercises local-variable type inference: `local = Foo.new; local.method`.
    def refund!(amount)
      local_gateway = PaymentGateway.new("EUR")
      local_gateway.authorize(amount)
    end

    def valid?
      true
    end

    def self.build_default
      new
    end

    class << self
      def instance_count
        @@instances
      end
    end

    alias total_for calculate_total

    private

    def finalize
      audit_trail
    end

    protected

    def audit_trail
      true
    end
  end
end

module Admin
  class Console
    private def secret
      MAX_RETRIES
    end

    def run
      secret
    end
  end
end

class Admin::Report
  def render
    normalize_amount(1.005)
  end
end

# Exercises retroactive visibility: `private :method_name` applied after definition.
class Admin::Permissions
  def check_access
    true
  end

  def verify_token
    true
  end

  def public_info
    true
  end

  private :check_access, :verify_token
end
