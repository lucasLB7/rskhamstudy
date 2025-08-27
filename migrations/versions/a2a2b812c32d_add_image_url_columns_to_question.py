from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a2a2b812c32d'
down_revision = None  # keep whatever is already in your file
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('question', sa.Column('question_image_url', sa.Text(), nullable=True))
    op.add_column('question', sa.Column('choice_a_image_url', sa.Text(), nullable=True))
    op.add_column('question', sa.Column('choice_b_image_url', sa.Text(), nullable=True))
    op.add_column('question', sa.Column('choice_c_image_url', sa.Text(), nullable=True))
    op.add_column('question', sa.Column('choice_d_image_url', sa.Text(), nullable=True))
    # NOTE: we are NOT altering the nullability of 'text' here on SQLite

def downgrade():
    op.drop_column('question', 'choice_d_image_url')
    op.drop_column('question', 'choice_c_image_url')
    op.drop_column('question', 'choice_b_image_url')
    op.drop_column('question', 'choice_a_image_url')
    op.drop_column('question', 'question_image_url')
